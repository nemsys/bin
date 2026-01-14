import re
import argparse

parser = argparse.ArgumentParser(description="Extract storage usage from MEGA log file.")
parser.add_argument("logfile", help="Path to the MEGA log file")
args = parser.parse_args()

with open(args.logfile, encoding='utf-8') as f:
    content = f.read()

pattern = re.compile(
    r'([^\s]+@gmail\.com).*?USED STORAGE:[^\n]*?(\d+\.\d+%) of ([\d\.]+ GB)',
row_number = 1
for match in pattern.finditer(content):
    email = match.group(1)
    percent = match.group(2)
    quota = match.group(3)
    print(f"{row_number:3} {email:50s} {percent:>8} {quota:>12}")
    row_number += 1
    email = match.group(1)
    percent = match.group(2)
    quota = match.group(3)
    print(f"{num:3} {email:50s} {percent:>8} {quota:>12}")
    num += 1
