import requests


def load_lockers():
    lockersResponse = requests.get(
        "https://raw.githubusercontent.com/stake-dao/api/main/api/lockers/index.json"
    )
    if lockersResponse.status_code != 200:
        return {}

    return lockersResponse.json()["parsed"]
