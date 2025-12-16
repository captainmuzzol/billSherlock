import requests
import sys

def check_ollama(host="http://localhost:11434"):
    print(f"Checking Ollama connection at {host}...")
    
    # 1. Check basic connectivity (version endpoint)
    try:
        resp = requests.get(f"{host}/api/version", timeout=5)
        if resp.status_code == 200:
            print(f"[OK] Ollama is reachable. Version: {resp.json().get('version')}")
        else:
            print(f"[WARN] Ollama responded with status {resp.status_code}: {resp.text}")
    except requests.exceptions.ConnectionError:
        print(f"[ERROR] Could not connect to {host}. Connection refused.")
        print("Suggestion: Ensure Ollama is running and listening on the expected port.")
        if "localhost" in host:
             print("If Ollama is in a container or VM, 'localhost' might not be reachable. Try the host IP.")
        return
    except Exception as e:
        print(f"[ERROR] Unexpected error checking version: {e}")
        return

    # 2. Check model availability
    model_name = "qwen3:1.7b"
    print(f"\nChecking for model '{model_name}'...")
    try:
        resp = requests.get(f"{host}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = [m['name'] for m in resp.json().get('models', [])]
            print(f"Available models: {models}")
            
            # Fuzzy match or exact match
            if any(model_name in m for m in models):
                print(f"[OK] Model '{model_name}' found.")
            else:
                print(f"[WARN] Model '{model_name}' NOT found in the list.")
                print("Suggestion: Run `ollama pull qwen3:1.7b`")
        else:
            print(f"[WARN] Could not fetch tags: {resp.status_code}")
    except Exception as e:
        print(f"[ERROR] Error checking tags: {e}")

    # 3. Test generation
    print(f"\nTesting generation with '{model_name}'...")
    try:
        payload = {
            "model": model_name,
            "prompt": "Hi",
            "stream": False
        }
        resp = requests.post(f"{host}/api/generate", json=payload, timeout=20)
        if resp.status_code == 200:
            print(f"[OK] Generation successful. Response: {resp.json().get('response')}")
        else:
            print(f"[ERROR] Generation failed: {resp.status_code} - {resp.text}")
    except Exception as e:
        print(f"[ERROR] Generation error: {e}")

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:11434"
    check_ollama(host)
