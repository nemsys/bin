import json
import glob
import os

path = os.path.expanduser('~/.config/BraveSoftware/Brave-Browser/*/Preferences')
files = glob.glob(path)

for f_path in files:
    if "System Profile" in f_path: continue
    with open(f_path, 'r') as f:
        try:
            data = json.load(f)
            # This is where custom names like "phoneiep" are usually stored
            name = data.get('profile', {}).get('name')
            
            # check the legacy name field if the first one is generic
            if not name or name == "Personal":
                name = data.get('account_info', [{}])[0].get('full_name')
            
            folder = f_path.split('/')[-2]
            print(f"{folder}: {name}")
        except Exception:
            pass