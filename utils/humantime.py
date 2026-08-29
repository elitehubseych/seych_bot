

def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def format_duration(seconds):
    seconds = int(abs(seconds))
    if seconds < 60:
        return "меньше минуты"

    total_minutes = seconds // 60
    total_days = total_minutes // (60 * 24)
    hours = (total_minutes // 60) % 24
    minutes = total_minutes % 60

    years, days = divmod(total_days, 365)

    units = [
        (years, "год", "года", "лет"),
        (days, "день", "дня", "дней"),
        (hours, "час", "часа", "часов"),
        (minutes, "минута", "минуты", "минут"),
    ]
    non_zero = [unit for unit in units if unit[0] > 0]
    picked = non_zero[:2]
    if not picked:
        return "меньше минуты"
    return " ".join(f"{value} {plural(value, one, few, many)}" for value, one, few, many in picked)
