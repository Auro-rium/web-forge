"""Generates the offline fixture site (mini bookstore mirroring books.toscrape.com markup + mini wiki)."""
from pathlib import Path
ROOT = Path(__file__).parent / "site"
CATS = {
 "poetry": [("Soul Mate", "£15.60", 4), ("Sharp Objects of Verse", "£52.14", 2), ("The Moon Garden", "£22.50", 5), ("Winter Hymns", "£48.00", 3)],
 "travel": [("Roads to Nowhere", "£33.10", 3), ("Lonely Atlas", "£19.99", 5), ("Seven Harbours", "£44.25", 1)],
 "science": [("Entropy Tales", "£29.99", 4), ("Quantum Gardening", "£12.45", 2), ("Orbital Notes", "£37.80", 5)],
}
STARS = ["Zero","One","Two","Three","Four","Five"]
def side():
    li = "".join(f'<li><a href="/catalogue/category/{c}/index.html">{c.title()}</a></li>' for c in CATS)
    return f'<div class="side_categories"><ul class="nav nav-list"><li><a href="/index.html">Books</a><ul>{li}</ul></li></ul></div>'
def page(title, body):
    return f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title></head><body><header><a href="/index.html">Fixture Books</a> <form action="/index.html"><input type="text" placeholder="Search books" name="q"></form></header>{side()}<main>{body}</main></body></html>'
def card(t, p, r):
    return f'<article class="product_pod"><h3><a href="/catalogue/{t.lower().replace(" ","-")}.html" title="{t}">{t[:20]}</a></h3><p class="star-rating {STARS[r]}">rating</p><div class="product_price"><p class="price_color">{p}</p><p class="instock availability">In stock</p></div></article>'
(ROOT / "catalogue/category").mkdir(parents=True, exist_ok=True)
allb = [b for v in CATS.values() for b in v]
(ROOT / "index.html").write_text(page("All products | Fixture Books", "<h1>All products</h1><ol class='row'>" + "".join(f"<li>{card(*b)}</li>" for b in allb) + "</ol>"))
for c, books in CATS.items():
    d = ROOT / "catalogue/category" / c; d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(page(f"{c.title()} | Fixture Books", f"<h1>{c.title()}</h1><ol class='row'>" + "".join(f"<li>{card(*b)}</li>" for b in books) + "</ol>"))
for t, p, r in allb:
    (ROOT / "catalogue" / f"{t.lower().replace(' ','-')}.html").write_text(page(t, f"<h1>{t}</h1><p class='price_color'>{p}</p><p>Rating: {STARS[r]} stars</p><p>A fixture description for {t}.</p>"))
W = ROOT / "wiki"; W.mkdir(exist_ok=True)
people = {"Ada_Lovelace": "Ada Lovelace (1815-1852) was an English mathematician known for her work on Charles Babbage's Analytical Engine.",
          "Alan_Turing": "Alan Turing (1912-1954) was an English mathematician and computer scientist, a founder of theoretical computer science."}
(W / "index.html").write_text(page("Mini Wiki", "<h1>Mini Wiki</h1><input type='text' placeholder='Search wiki'><ul>" + "".join(f"<li><a href='/wiki/{k}.html'>{k.replace('_',' ')}</a></li>" for k in people) + "</ul>"))
for k, v in people.items():
    (W / f"{k}.html").write_text(page(k.replace("_", " "), f"<h1>{k.replace('_',' ')}</h1><p class='summary'>{v}</p>"))
print("fixtures built")

# data/specs.html (table + JSON-LD + meta, for the generic extraction tools) is hand-written and checked in.
