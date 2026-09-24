class CloudflareDetection:
    @staticmethod
    def looks_like_cf_html(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        return (
            "<title>just a moment" in t
            or "/cdn-cgi/challenge-platform/" in t
            or "enable javascript and cookies to continue" in t
        )

# ⬇️ Add this shim so you can `from ... import looks_like_cf_html`
def looks_like_cf_html(text: str) -> bool:
    return CloudflareDetection.looks_like_cf_html(text)


def looks_like_cf_block(text: str) -> bool:
    """Cloudflare WAF / bot-management BLOCK page ("Attention Required! |
    Cloudflare ... you have been blocked"). Unlike the "Just a moment"
    challenge, no cookie mint clears it: the exit IP or the client itself is
    refused, so callers should back off rather than re-mint."""
    if not text:
        return False
    t = text.lower()
    return ("<title>attention required! | cloudflare</title>" in t
            or ("cloudflare" in t and "you have been blocked" in t))
