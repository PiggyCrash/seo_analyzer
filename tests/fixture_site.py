"""A tiny local website that simulates real-world crawl situations.

    python tests/fixture_site.py            # serves on http://127.0.0.1:8765
    python main.py 127.0.0.1:8765 --no-llm --force

Pages: normal Webflow-like homepage, about, services, blog + posts, a Cloudflare-style
challenge, a 429, a soft 404, a noindex page, an age gate and a login redirect.
FIXTURE_MODE=blocked makes every page (except robots.txt) a Cloudflare challenge.
"""
from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = """<!doctype html><html lang="en" data-wf-site="abc123"><head>
<title>Zero Penny - Full Stack Performance Marketing</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-J236NSVR7Z"></script>
<script>gtag('config','G-J236NSVR7Z');</script>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-K5MF2VXR');</script>
<script>!function(f,b,e,v){}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');fbq('init','123456789012345');</script>
<script>ttq.load('CABCDEFGHIJ1234567');</script>
</head><body>
<nav><a href="/about">About</a><a href="/services">Services</a><a href="/blog">Blog</a>
<a href="/contact">Contact</a><a href="/login">Login</a></nav>
<h1>Ads That Work - Even in "No-Go" Industries</h1>
<h2>The problem</h2><p>{filler}</p><h2>Proof</h2><p>{filler}</p><h4>Skipped level</h4>
<img src="/a.png"><img src="/b.png" alt="Team photo"><img src="/c.png" alt="">
<a href="/blog/crypto-ads-guide?utm_source=x">Crypto ads guide</a>
<a href="/blog/gambling-meta-ads">Gambling on Meta</a>
<a href="/protected">Protected</a><a href="/busy">Busy</a><a href="/old-page">Old</a>
<a href="/noindex-page">Hidden</a><a href="/age">Age</a><a href="/portal">Portal</a>
<a href="/brochure.pdf">PDF</a><a href="https://twitter.com/x">X</a><a href="mailto:a@b.c">Mail</a>
</body></html>"""

PAGE = """<!doctype html><html lang="en"><head><title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="http://127.0.0.1:8765{path}">
<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}">
{extra}</head><body><header><a href="/">Home</a></header><main><h1>{title}</h1><h2>Section</h2>
<p>{body}</p></main><footer>Footer links</footer></body></html>"""

FILLER = " ".join(["restricted niche advertising for crypto gambling cannabis and peptide brands"] * 30)
POST_SCHEMA = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"BlogPosting","headline":"x","publisher":{"@type":"Organization","name":"Zero Penny"}}</script>'

CHALLENGE = "<html><head><title>Just a moment...</title></head><body><script>window._cf_chl_opt={};</script></body></html>"
AGE = "<html><head><title>Verify your age</title></head><body><h1>Are you over 18?</h1><button>Yes</button></body></html>"
SOFT404 = "<html><head><title>Page not found</title></head><body><h1>Oops! Page not found</h1></body></html>"

ROBOTS = "User-agent: *\nDisallow: /private\nSitemap: http://127.0.0.1:8765/sitemap.xml\n"
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>http://127.0.0.1:8765/</loc></url><url><loc>http://127.0.0.1:8765/about</loc></url>
<url><loc>http://127.0.0.1:8765/pricing</loc></url><url><loc>http://127.0.0.1:8765/blog/ad-account-ban-recovery</loc></url>
<url><loc>http://127.0.0.1:8765/private/secret</loc></url></urlset>"""


def page(path, title, desc="A clear, specific description of this page for search engines and social previews.",
         extra="", words=FILLER):
    return PAGE.format(path=path, title=title, desc=desc, extra=extra, body=words)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to, code=301):
        self.send_response(code)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        routes = {
            "/": lambda: self.send(200, HOME.replace("{filler}", FILLER)),
            "/robots.txt": lambda: self.send(200, ROBOTS, "text/plain"),
            "/sitemap.xml": lambda: self.send(200, SITEMAP, "application/xml"),
            "/about": lambda: self.send(200, page(p, "About Zero Penny | Restricted Niche Ad Agency")),
            "/services": lambda: self.send(200, page(p, "Services", desc="Short")),
            "/pricing": lambda: self.send(200, page(p, "Pricing plans for crypto and gambling advertising")),
            "/contact": lambda: self.send(200, page(p, "Contact us about your restricted niche campaigns", words="Email us.")),
            "/blog": lambda: self.send(200, page(p, "Zero to One - the restricted niche advertising blog")),
            "/blog/crypto-ads-guide": lambda: self.send(200, page(p, "How to run crypto ads without getting banned", extra=POST_SCHEMA)),
            "/blog/gambling-meta-ads": lambda: self.send(200, page(p, "Gambling ads on Meta: what actually works in 2026")),
            "/blog/ad-account-ban-recovery": lambda: self.send(200, page(p, "Ad account ban recovery: a practical playbook", extra=POST_SCHEMA)),
            "/protected": lambda: self.send(403, CHALLENGE, headers={"cf-mitigated": "challenge"}),
            "/busy": lambda: self.send(429, "slow down", headers={"Retry-After": "1"}),
            "/old-page": lambda: self.send(200, SOFT404),
            "/noindex-page": lambda: self.send(200, page(p, "Internal thank you page for form submissions",
                                                         extra='<meta name="robots" content="noindex, follow">')),
            "/age": lambda: self.send(200, AGE),
            "/portal": lambda: self.redirect("/login"),
            "/login": lambda: self.send(200, "<html><body><form><input type=password></form></body></html>"),
            "/brochure.pdf": lambda: self.send(200, "%PDF", "application/pdf"),
        }
        if os.getenv("FIXTURE_MODE") == "blocked" and p != "/robots.txt":
            return self.send(403, CHALLENGE, headers={"cf-mitigated": "challenge"})
        handler = routes.get(p)
        if handler:
            handler()
        else:
            self.send(404, "<html><body><h1>404</h1></body></html>")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"fixture site on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
