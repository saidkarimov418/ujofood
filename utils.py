import math, random, string

def fmt_price(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"

def rand_code(n=8):
    import random, string
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlmb/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
