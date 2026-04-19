import requests
from requests.auth import HTTPBasicAuth

# --- YOUR SPECIFIC DATA ---
DB_MAC = "1CCAE3761FFC"
INTERCOM_ID = "ghurhs"
USER = "ghurhs0000" #regular user "ghurhs0018" # Replace with your actual user (e.g., ghurhs0001)
PASS = "nVVN7uFE5Q" # regular user pwd"aviGate8844"
# The ID must be part of the path to prevent the 404 routing error
BASE_URL = f"https://api.doorbird.io/{INTERCOM_ID}/bha-api"
# The Cloud URL *must* include the ID in the path to avoid a 404

def open_gate():
    try:
        # 1. AUTHENTICATE
        print(f"Connecting to Cloud Proxy for {INTERCOM_ID}...")
        login_url = f"{BASE_URL}/login.cgi"
        
        # Use Basic Auth as 'normal' API practice
        auth = HTTPBasicAuth(USER, PASS)
        r = requests.get(login_url, auth=auth, timeout=10)
        
        if r.status_code == 404:
            print("ERROR 404: The cloud cannot route to your device. Check your Intercom ID.")
            return

        # 2. CAPTURE SESSION
        if "SESSIONID=" in r.text:
            session_id = r.text.split("SESSIONID=")[1].strip()
            print(f"Session Obtained: {session_id[:8]}...")
            
            # 3. TRIGGER RELAY 1
            # 'r=1' is the gate relay
            gate_url = f"{BASE_URL}/open-door.cgi"
            params = {"sessionid": session_id, "r": "1"}
            
            resp = requests.get(gate_url, params=params)
            if "BHA-RETURN: OK" in resp.text:
                print("🚀 SUCCESS: Gate is opening!")
            else:
                print(f"FAILED: {resp.text}")
        else:
            print(f"LOGIN FAILED: {r.text}")

    except Exception as e:
        print(f"SYSTEM ERROR: {e}")

if __name__ == "__main__":
    open_gate()