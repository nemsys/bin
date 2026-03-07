import re

with open('/data/bin/meganz/logs/mega-2025-09.log', encoding='utf-8') as f:
    content = f.read()

pattern = re.compile(
    r'([^\s]+@gmail\.com).*?USED STORAGE:[^\n]*?(\d+\.\d+%) of ([\d\.]+ GB)',
    re.DOTALL
)

print(f"{'No.':>3} {'Account':50s} {'Used':>8} {'Quota':>12}")
print('-' * 75)
num = 1
for match in pattern.finditer(content):
    email = match.group(1)
    percent = match.group(2)
    quota = match.group(3)
    print(f"{num:3} {email:50s} {percent:>8} {quota:>12}")
    num += 1
