import requests

# Toast Authentication endpoint
AUTH_URL = "https://ws-api.toasttab.com/authentication/v1/authentication/login"

def get_access_token(client_id, client_secret, user_access_type="TOAST_MACHINE_CLIENT", timeout=10):
    payload = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "userAccessType": user_access_type,
    }
    # Request token
    resp = requests.post(AUTH_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    # Extract accessToken
    token = data.get("token", {}).get("accessToken")
    if token:
        return token
    else:
        error = data.get("error", "")
        raise Exception(f"Authentication failed: {resp.status_code} {error}")