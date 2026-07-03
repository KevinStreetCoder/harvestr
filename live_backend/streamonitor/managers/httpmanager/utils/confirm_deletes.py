from parameters import WEB_CONFIRM_DELETES


def confirm_deletes(user_agent: str):
    # request.headers.get('User-Agent') is None when a client sends no UA header
    # (health checks, curl, bots) -> .lower() on None raised AttributeError and
    # 500'd the route ~7800x. Coerce to "" so no-UA requests are simply "not mobile".
    ua = (user_agent or "").lower()
    mobile_strings = ['android', 'iphone', 'ipad', 'mobile']
    if WEB_CONFIRM_DELETES and WEB_CONFIRM_DELETES != "MOBILE":
        return True
    elif WEB_CONFIRM_DELETES:
        return any(mobile in ua for mobile in mobile_strings)
    else:
        return False