#!/usr/bin/env python3
"""Fashion Agent : base ventes mode (Homme/Femme/Enfant), segmentation micro/macro, mailing.
Bibliothèque standard uniquement (SQLite). Usage : python fashion_agent.py --help"""
import argparse, csv, os, random, smtplib, sqlite3, sys, time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from string import Template

DB = Path(os.environ.get("FASHION_DB", "fashion.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE, first_name TEXT, last_name TEXT,
  gender TEXT, birth_date TEXT, city TEXT, country TEXT DEFAULT 'FR', created_at TEXT,
  consent_marketing INTEGER DEFAULT 0, unsubscribed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, sku TEXT UNIQUE, name TEXT, brand TEXT,
  department TEXT CHECK(department IN ('Homme','Femme','Enfant')),
  category TEXT, price REAL, cost REAL);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),
  order_date TEXT, channel TEXT, total REAL);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id),
  product_id INTEGER REFERENCES products(id), qty INTEGER, unit_price REAL);
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, subject TEXT, filters TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS campaign_sends(
  id INTEGER PRIMARY KEY, campaign_id INTEGER REFERENCES campaigns(id),
  customer_id INTEGER REFERENCES customers(id), status TEXT, error TEXT, sent_at TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_c ON orders(customer_id, order_date);
CREATE INDEX IF NOT EXISTS ix_items_o ON order_items(order_id);
"""

SEGMENTS = {  # macro-segments RFM
    "champions": "Champions (récents, fréquents, gros paniers)",
    "loyaux": "Loyaux (achètent souvent)",
    "nouveaux": "Nouveaux / prometteurs (récents, peu de commandes)",
    "a_risque": "À risque (étaient fréquents, silencieux)",
    "dormants": "Dormants (anciens, peu actifs)",
    "occasionnels": "Occasionnels",
}


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ----------------------------------------------------------------- données démo
def seed(con, n_customers):
    random.seed(42)
    today = date.today()
    brands = {"Zara": 1.0, "H&M": 0.6, "Uniqlo": 0.7, "Mango": 0.9, "Ralph Lauren": 2.5,
              "Lacoste": 2.2, "Levi's": 1.5, "Kiabi": 0.5}
    cats = {"Homme": {"T-shirt": 25, "Chemise": 50, "Jean": 70, "Pull": 60, "Veste": 120, "Costume": 250},
            "Femme": {"Robe": 70, "Jupe": 45, "Chemisier": 50, "Jean": 70, "Manteau": 140, "Pull": 60},
            "Enfant": {"T-shirt": 15, "Pantalon": 30, "Robe": 35, "Sweat": 30, "Blouson": 60}}
    n = 0
    for dep, cs in cats.items():
        for cat, base in cs.items():
            for b, tier in brands.items():
                n += 1
                price = round(base * tier * random.uniform(0.9, 1.2), 2)
                con.execute("INSERT OR IGNORE INTO products(sku,name,brand,department,category,price,cost) VALUES(?,?,?,?,?,?,?)",
                            (f"{b[:3].upper()}-{dep[0]}-{cat[:3].upper()}-{n}", f"{cat} {b} {dep}", b, dep, cat, price, round(price * .45, 2)))
    prods = [dict(r) for r in con.execute("SELECT * FROM products")]
    fm = ["Camille", "Léa", "Emma", "Chloé", "Sarah", "Inès", "Julie", "Manon", "Laura", "Zoé"]
    ml = ["Lucas", "Hugo", "Louis", "Nathan", "Théo", "Paul", "Adam", "Jules", "Max", "Karim"]
    last = ["Martin", "Bernard", "Dubois", "Moreau", "Laurent", "Simon", "Michel", "Garcia", "Roux", "Fournier"]
    cities = ["Paris", "Lyon", "Marseille", "Lille", "Bordeaux", "Nantes", "Toulouse", "Strasbourg"]
    strip = str.maketrans("éèêëàâîïôùûç", "eeeeaaiiouuc")
    for i in range(n_customers):
        g = random.choice("FM")
        fn = random.choice(fm if g == "F" else ml)
        ln = random.choice(last)
        created = today - timedelta(days=random.randint(30, 730))
        birth = date(random.randint(1955, 2005), random.randint(1, 12), random.randint(1, 28))
        email = f"{fn}.{ln}{i}@example.com".lower().translate(strip)
        cur = con.execute("INSERT INTO customers(email,first_name,last_name,gender,birth_date,city,created_at,consent_marketing) VALUES(?,?,?,?,?,?,?,?)",
                          (email, fn, ln, g, birth.isoformat(), random.choice(cities), created.isoformat(), int(random.random() < .85)))
        cid = cur.lastrowid
        w = {"F": [.1, .6, .3], "M": [.6, .1, .3]}[g]
        pref_dep = random.choices(["Homme", "Femme", "Enfant"], w)[0]
        pref_brand = random.choice(list(brands))
        span = max((today - created).days, 1)
        n_orders = min(15, 1 + int(random.expovariate(0.6)))
        for d in sorted(random.randint(0, span) for _ in range(n_orders)):
            items, total = [], 0
            for _ in range(random.randint(1, 4)):
                pool = [p for p in prods if p["department"] == pref_dep] if random.random() < .65 else prods
                if random.random() < .5:
                    pool = [p for p in pool if p["brand"] == pref_brand] or pool
                p = random.choice(pool)
                q = random.choice([1, 1, 1, 2])
                items.append((p["id"], q, p["price"]))
                total += q * p["price"]
            o = con.execute("INSERT INTO orders(customer_id,order_date,channel,total) VALUES(?,?,?,?)",
                            (cid, (created + timedelta(days=d)).isoformat(), random.choice(["web", "web", "magasin"]), round(total, 2)))
            con.executemany("INSERT INTO order_items(order_id,product_id,qty,unit_price) VALUES(?,?,?,?)",
                            [(o.lastrowid, *it) for it in items])
    con.commit()


# ----------------------------------------------------------------- segmentation
def _scores(values, reverse=False):
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=reverse)
    out = [0] * len(values)
    for rank, i in enumerate(order):
        out[i] = 1 + rank * 5 // len(values)
    return out


def rfm(con):
    """Macro-segmentation RFM sur toute la base : {customer_id: {...}}"""
    rows = con.execute("SELECT customer_id id, MAX(order_date) last, COUNT(*) f, SUM(total) m FROM orders GROUP BY customer_id").fetchall()
    if not rows:
        return {}
    today = date.today()
    days = [(today - date.fromisoformat(r["last"])).days for r in rows]
    R = _scores(days, reverse=True)  # plus récent = score élevé
    F = _scores([r["f"] for r in rows])
    M = _scores([r["m"] for r in rows])
    res = {}
    for r, d, a, b, c in zip(rows, days, R, F, M):
        if a >= 4 and b >= 4 and c >= 4: s = "champions"
        elif b >= 4 and a >= 3: s = "loyaux"
        elif a >= 4 and b <= 2: s = "nouveaux"
        elif a <= 2 and b >= 3: s = "a_risque"
        elif a <= 2: s = "dormants"
        else: s = "occasionnels"
        res[r["id"]] = dict(recency_days=d, R=a, F=b, M=c, rfm_segment=s)
    return res


def select(con, a):
    """Micro-segmentation : filtres combinables. Les filtres produit (marque/rayon/catégorie)
    restreignent aussi CA, nb de commandes et dernière commande à ce périmètre."""
    t = date.today()
    w, p, h, hp = [], [], [], []
    for col, tbl in (("department", "p"), ("brand", "p"), ("category", "p"), ("city", "c"), ("gender", "c")):
        v = getattr(a, col, None)
        if v:
            w.append(f"{tbl}.{col} = ? COLLATE NOCASE"); p.append(v)
    if a.age_min is not None:
        w.append("c.birth_date <= ?"); p.append(date(t.year - a.age_min, t.month, min(t.day, 28)).isoformat())
    if a.age_max is not None:
        w.append("c.birth_date > ?"); p.append(date(t.year - a.age_max - 1, t.month, min(t.day, 28)).isoformat())
    if a.since: w.append("o.order_date >= ?"); p.append(a.since)
    if a.until: w.append("o.order_date <= ?"); p.append(a.until)
    if a.min_spend is not None: h.append("spend >= ?"); hp.append(a.min_spend)
    if a.max_spend is not None: h.append("spend <= ?"); hp.append(a.max_spend)
    if a.min_orders is not None: h.append("orders >= ?"); hp.append(a.min_orders)
    if a.inactive_days is not None: h.append("last_order <= ?"); hp.append((t - timedelta(days=a.inactive_days)).isoformat())
    if a.active_days is not None: h.append("last_order >= ?"); hp.append((t - timedelta(days=a.active_days)).isoformat())
    sql = f"""SELECT c.id, c.email, c.first_name, c.last_name, c.gender, c.birth_date, c.city,
      c.consent_marketing, c.unsubscribed, COUNT(DISTINCT o.id) orders,
      ROUND(SUM(oi.qty*oi.unit_price),2) spend, MAX(o.order_date) last_order
      FROM customers c JOIN orders o ON o.customer_id=c.id
      JOIN order_items oi ON oi.order_id=o.id JOIN products p ON p.id=oi.product_id
      WHERE {' AND '.join(w) or '1=1'} GROUP BY c.id
      {'HAVING ' + ' AND '.join(h) if h else ''} ORDER BY spend DESC"""
    rf = rfm(con)
    out = []
    for r in con.execute(sql, p + hp):
        d = dict(r)
        d["age"] = t.year - int(d.pop("birth_date")[:4])
        d.update(rf.get(d["id"], {}))
        if a.segment and d.get("rfm_segment") != a.segment:
            continue
        out.append(d)
    return out


# ----------------------------------------------------------------- commandes
def cmd_stats(con, a):
    q = lambda s: con.execute(s).fetchall()
    print("== CA par rayon ==")
    for r in q("SELECT p.department d, ROUND(SUM(oi.qty*oi.unit_price)) ca, SUM(oi.qty) pieces FROM order_items oi JOIN products p ON p.id=oi.product_id GROUP BY d ORDER BY ca DESC"):
        print(f"  {r['d']:<8} {r['ca']:>10.0f} €  {r['pieces']:>6} pièces")
    print("== CA par marque ==")
    for r in q("SELECT p.brand b, ROUND(SUM(oi.qty*oi.unit_price)) ca FROM order_items oi JOIN products p ON p.id=oi.product_id GROUP BY b ORDER BY ca DESC"):
        print(f"  {r['b']:<13} {r['ca']:>10.0f} €")
    print("== 6 derniers mois ==")
    for r in q("SELECT substr(order_date,1,7) m, COUNT(*) n, ROUND(SUM(total)) ca FROM orders GROUP BY m ORDER BY m DESC LIMIT 6"):
        print(f"  {r['m']}  {r['n']:>5} commandes  {r['ca']:>9.0f} €")
    c = q("SELECT COUNT(*) n, SUM(consent_marketing) opt, SUM(unsubscribed) un FROM customers")[0]
    print(f"== Clients : {c['n']} | opt-in marketing : {c['opt']} | désinscrits : {c['un']}")


def cmd_rfm(con, a):
    rf = rfm(con)
    spend = {r["customer_id"]: r["m"] for r in con.execute("SELECT customer_id, SUM(total) m FROM orders GROUP BY customer_id")}
    ca = sum(spend.values())
    print(f"{'segment':<14}{'clients':>8}{'CA moyen':>10}{'% du CA':>9}  description")
    for s, label in SEGMENTS.items():
        ids = [i for i, v in rf.items() if v["rfm_segment"] == s]
        m = sum(spend[i] for i in ids)
        print(f"{s:<14}{len(ids):>8}{(m / len(ids) if ids else 0):>10.0f}{100 * m / ca:>8.1f}%  {label}")


def cmd_export(con, a):
    rows = select(con, a)
    out = Path(a.out or f"exports/segment_{datetime.now():%Y%m%d_%H%M}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader(); wr.writerows(rows)
    print(f"{len(rows)} clients -> {out}")


def cmd_mailing(con, a):
    rows = [r for r in select(con, a) if r["consent_marketing"] and not r["unsubscribed"]]
    camp = con.execute("SELECT id FROM campaigns WHERE name=?", (a.name,)).fetchone()
    done = set()
    if camp:
        done = {r[0] for r in con.execute("SELECT customer_id FROM campaign_sends WHERE campaign_id=? AND status='sent'", (camp["id"],))}
    rows = [r for r in rows if r["id"] not in done]
    if a.limit: rows = rows[:a.limit]
    tpl = Template(Path(a.template).read_text(encoding="utf-8"))
    base = os.environ.get("UNSUBSCRIBE_BASE", "https://example.com/unsubscribe")

    def render(r):
        ctx = dict(first_name=r["first_name"], last_name=r["last_name"], email=r["email"],
                   unsubscribe_url=f"{base}?e={r['email']}")
        return Template(a.subject).safe_substitute(ctx), tpl.safe_substitute(ctx), ctx

    print(f"Campagne '{a.name}' : {len(rows)} destinataires éligibles (opt-in, non désinscrits, pas déjà envoyés)")
    if not rows: return
    if not a.send:
        Path("outbox").mkdir(exist_ok=True)
        _, html, _ = render(rows[0])
        Path("outbox/preview.html").write_text(html, encoding="utf-8")
        print("DRY-RUN : rien n'est envoyé. Aperçu -> outbox/preview.html. Ajoutez --send pour envoyer.")
        for r in rows[:5]: print("  ->", r["email"])
        return
    env = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM")}
    if not all(env.values()): sys.exit("Variables manquantes : SMTP_HOST, SMTP_USER, SMTP_PASSWORD, MAIL_FROM (SMTP_PORT optionnel)")
    if not camp:
        cur = con.execute("INSERT INTO campaigns(name,subject,filters,created_at) VALUES(?,?,?,?)",
                          (a.name, a.subject, " ".join(sys.argv[2:]), datetime.now().isoformat()))
        camp = {"id": cur.lastrowid}
    sent = 0
    with smtplib.SMTP(env["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls(); s.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        for r in rows:
            subj, html, ctx = render(r)
            m = EmailMessage()
            m["From"], m["To"], m["Subject"] = env["MAIL_FROM"], r["email"], subj
            m["List-Unsubscribe"] = f"<{ctx['unsubscribe_url']}>"
            m.set_content("Votre messagerie ne supporte pas le HTML.")
            m.add_alternative(html, subtype="html")
            try:
                s.send_message(m); st, err = "sent", None; sent += 1
            except Exception as e:
                st, err = "error", str(e)
            con.execute("INSERT INTO campaign_sends(campaign_id,customer_id,status,error,sent_at) VALUES(?,?,?,?,?)",
                        (camp["id"], r["id"], st, err, datetime.now().isoformat()))
            con.commit(); time.sleep(a.sleep)
    print(f"{sent}/{len(rows)} emails envoyés.")


def cmd_unsub(con, a):
    n = con.execute("UPDATE customers SET unsubscribed=1 WHERE lower(email)=lower(?)", (a.email,)).rowcount
    con.commit(); print("désinscrit" if n else "email introuvable")


# ----------------------------------------------------------------- CLI
def main():
    f = argparse.ArgumentParser(add_help=False)
    g = f.add_argument_group("filtres (micro/macro segmentation)")
    g.add_argument("--segment", choices=list(SEGMENTS), help="macro-segment RFM")
    g.add_argument("--department", help="Homme | Femme | Enfant")
    g.add_argument("--brand"); g.add_argument("--category"); g.add_argument("--city")
    g.add_argument("--gender", choices=["F", "M"])
    g.add_argument("--age-min", type=int); g.add_argument("--age-max", type=int)
    g.add_argument("--min-spend", type=float); g.add_argument("--max-spend", type=float)
    g.add_argument("--min-orders", type=int)
    g.add_argument("--inactive-days", type=int, help="dernière commande il y a >= N jours")
    g.add_argument("--active-days", type=int, help="dernière commande il y a <= N jours")
    g.add_argument("--since", help="AAAA-MM-JJ"); g.add_argument("--until", help="AAAA-MM-JJ")

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="crée la base vide")
    s = sub.add_parser("seed", help="charge des données de démonstration"); s.add_argument("--customers", type=int, default=600)
    sub.add_parser("stats", help="KPI ventes")
    sub.add_parser("rfm", help="macro-segmentation RFM")
    s = sub.add_parser("export", parents=[f], help="export CSV d'un segment"); s.add_argument("--out")
    s = sub.add_parser("mailing", parents=[f], help="envoi de campagne (dry-run par défaut)")
    s.add_argument("--name", required=True); s.add_argument("--subject", required=True)
    s.add_argument("--template", default="templates/promo.html")
    s.add_argument("--send", action="store_true"); s.add_argument("--limit", type=int)
    s.add_argument("--sleep", type=float, default=0.2)
    s = sub.add_parser("unsubscribe"); s.add_argument("email")
    a = ap.parse_args()
    con = connect()
    if a.cmd == "init": print(f"Base prête : {DB}")
    elif a.cmd == "seed": seed(con, a.customers); print("Données de démo chargées.")
    else: {"stats": cmd_stats, "rfm": cmd_rfm, "export": cmd_export, "mailing": cmd_mailing, "unsubscribe": cmd_unsub}[a.cmd](con, a)


if __name__ == "__main__":
    main()
