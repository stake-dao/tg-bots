def abbreviate_number(num, force_decimals=False):
    if abs(num) >= 1_000_000_000:
        return f"{num / 1_000_000_000:.2f}B"
    elif abs(num) >= 1_000_000:
        return f"{num / 1_000_000:.2f}M"
    elif abs(num) >= 1_000:
        return f"{num / 1_000:.2f}k"
    else:
        if force_decimals == True:
            return f"{num:.2f}"
        else:
            return f"{num:.0f}"
