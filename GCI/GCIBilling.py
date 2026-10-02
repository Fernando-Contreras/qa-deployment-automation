from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import os
import subprocess
from collections import OrderedDict
import sys

# La contraseña del servidor ya no vive en el código: sale del almacén cifrado
# (py NTT\Setup_Credentials.py gci_unix). La primera vez la pide y la guarda.
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NTT"))
from nc_credentials import get_credentials  # noqa: E402

UNIX_USER, UNIX_PASSWORD = get_credentials("gci_unix", default_username="you")

ENV_MAP = {
    "UAT1": "infgca7",
    "UAT2": "infgca5",
    "UAT3": "infgca1",
    "NC2": "infgca3",
    "B2B": "infgca3",
}

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

def parse_date(date_str):
    """Parse date in format YYYYMMDD or DD-MMM-YY"""
    date_str = date_str.strip()
    
    # Try YYYYMMDD format first
    if len(date_str) == 8 and date_str.isdigit():
        return datetime.strptime(date_str, "%Y%m%d")
    
    # Try DD-MMM-YY format
    try:
        return datetime.strptime(date_str, "%d-%b-%y")
    except ValueError:
        pass
    
    raise ValueError(f"Invalid date format: {date_str}")

def parse_month_year_or_count(input_str):
    """
    Parse input that can be either:
    - A number (1, 2, 3, etc.) = number of billing cycles
    - Month and year (e.g., 'feb 2026', 'dec 2025')
    
    Returns:
    - (mode='count', value=N) if it's a number
    - (mode='month_year', month=M, year=Y) if it's month/year
    """
    input_str = input_str.strip().lower()
    
    # Check if it's just a number
    if input_str.isdigit():
        count = int(input_str)
        if count < 1:
            raise ValueError("Number of billings must be at least 1")
        return {'mode': 'count', 'value': count}
    
    # Otherwise, try to parse as month year
    parts = input_str.split()
    
    if len(parts) != 2:
        raise ValueError("Format should be: NUMBER (e.g., '3') OR 'MONTH YEAR' (e.g., 'feb 2026')")
    
    month_str = parts[0]
    year_str = parts[1]
    
    if month_str not in MONTH_MAP:
        raise ValueError(f"Invalid month: {month_str}")
    
    try:
        year = int(year_str)
    except ValueError:
        raise ValueError(f"Invalid year: {year_str}")
    
    return {'mode': 'month_year', 'month': MONTH_MAP[month_str], 'year': year}


def copy_to_clipboard(text):
    try:
        subprocess.run(["clip"], input=text, text=True, check=True)
        print("📋 First SQL copied to clipboard.")
    except Exception as e:
        print(f"⚠️ Could not copy first SQL to clipboard: {e}")

print("Environments: UAT1, UAT2, UAT3, NC2, B2B")
env_choice = input("Environment: ").strip().upper()

if env_choice not in ENV_MAP:
    print("❌ Invalid environment.")
    exit(1)

infgca = ENV_MAP[env_choice]
AUTO_FOLDERS = {"UAT1": "alex", "UAT2": "Alex"}

if env_choice in AUTO_FOLDERS:
    folder = AUTO_FOLDERS[env_choice]
    print(f"Folder: {folder} (auto for {env_choice})")
else:
    folder = input("Folder (e.g. Alex): ").strip()
ticket = input("Ticket number (e.g. 16540): ").strip()

# First, collect account numbers
print("\nEnter account numbers (comma separated OR one per line).")
print("Finish with empty line:")
accounts = []
while True:
    line = input().strip()
    if not line:
        break
    parts = [p.strip() for p in line.split(",") if p.strip()]
    accounts.extend(parts)

if not accounts:
    print("❌ No accounts provided.")
    exit(1)

# Print SQL to check NBDs
first_sql_lines = ["SELECT * FROM account WHERE account_num in ("]
for i, acc in enumerate(accounts):
    if i == len(accounts) - 1:
        first_sql_lines.append(f"'{acc}'")
    else:
        first_sql_lines.append(f"'{acc}',")
first_sql_lines.append(") order by NEXT_BILL_DTM asc;")
first_sql = "\n".join(first_sql_lines)

print("\nRun this SQL to check NBDs:\n")
print(first_sql)
print("")
copy_to_clipboard(first_sql)

# Ask if there are multiple NBDs (only makes sense with more than one account)
if len(accounts) == 1:
    multiple_nbds = 'n'
else:
    multiple_nbds = input("Are there multiple NBDs? (y/n): ").strip().lower()

account_dates = OrderedDict()  # {account_num: nbd_date} - preserves insertion order

if multiple_nbds == 'y':
    print("\nEnter account numbers with dates (format: ACCOUNT_NUM DATE)")
    print("Date can be YYYYMMDD or DD-MMM-YY (e.g., 1100353036 20251101 or 1100353036 01-NOV-25)")
    print("Finish with empty line:")
    
    while True:
        line = input().strip()
        if not line:
            break
        
        parts = line.split()
        if len(parts) < 2:
            print("⚠️ Invalid format. Expected: ACCOUNT_NUM DATE")
            continue
        
        account_num = parts[0].strip()
        date_str = parts[1].strip()
        
        try:
            nbd_date = parse_date(date_str)
            account_dates[account_num] = nbd_date
        except ValueError as e:
            print(f"⚠️ {e}")
            continue
    
    if not account_dates:
        print("❌ No valid accounts provided.")
        exit(1)
    
    # Update accounts list to match the order from account_dates
    accounts = list(account_dates.keys())
    
else:  # Single NBD for all accounts
    nbd_str = input("\nEnter NBD (YYYYMMDD or DD-MMM-YY): ").strip()
    try:
        single_nbd = parse_date(nbd_str)
        # Assign same NBD to all accounts
        for acc in accounts:
            account_dates[acc] = single_nbd
    except ValueError:
        print("❌ Invalid NBD format.")
        exit(1)

# Ask for billing cycles (number or month/year)
while True:
    print("\nHow many billing cycles to generate?")
    print("  - Enter a NUMBER (e.g., 1, 2, 3) for that many billing cycles")
    print("  - OR enter MONTH YEAR (e.g., 'dec 2026', 'feb 2027') to bill until that month")
    billing_input = input("Your choice: ").strip()
    
    try:
        billing_config = parse_month_year_or_count(billing_input)
        break
    except ValueError as e:
        print(f"❌ {e}")

today_str = datetime.today().strftime("%Y%m%d")

# Generate all unique billing dates for all accounts
all_bill_dates = set()
account_bill_dates = {}  # {account_num: [date1, date2, ...]}

for account, nbd_date in account_dates.items():
    bill_dates = []
    current_date = nbd_date
    billing_count = 0
    
    while True:
        bill_dates.append(current_date)
        all_bill_dates.add(current_date)
        billing_count += 1
        
        # Check stopping condition based on mode
        if billing_config['mode'] == 'count':
            # Stop after N billing cycles
            if billing_count >= billing_config['value']:
                break
        else:  # mode == 'month_year'
            # Stop when we reach or pass the target month/year
            end_month = billing_config['month']
            end_year = billing_config['year']
            if current_date.year > end_year or (current_date.year == end_year and current_date.month >= end_month):
                break
        
        # Move to next month
        current_date = current_date + relativedelta(months=1)
    
    account_bill_dates[account] = bill_dates

# Sort all unique billing dates
sorted_bill_dates = sorted(list(all_bill_dates))

# Get last billing date
last_bd = sorted_bill_dates[-1]
last_bd_str = last_bd.strftime("%Y%m%d")
next_bd = (last_bd + timedelta(days=1)).strftime("%Y%m%d")

# Group accounts by their INITIAL billing date (NBD) to process them together
nbd_groups = OrderedDict()  # {nbd_date: [accounts]}
for account, nbd_date in account_dates.items():
    if nbd_date not in nbd_groups:
        nbd_groups[nbd_date] = []
    nbd_groups[nbd_date].append(account)

accounts_csv = ", ".join(accounts)
base_path = f"/home/{infgca}/{folder}/{ticket}"

# Output file
out_path = r"C:\Users\you\Documents\GCI JF\Notes\Ultimate_Billing.txt"
os.makedirs(os.path.dirname(out_path), exist_ok=True)

lines = []

def o(s=""):
    print(s)
    lines.append(s)

# ================= OUTPUT =================
o("0) Data for the whole process")
o(base_path)
o(f"ACCS: {accounts_csv}")
o(f"Today's date: {today_str}")

if multiple_nbds == 'y':
    nbds_str = ", ".join([d.strftime("%Y%m%d") for d in sorted_bill_dates])
    o(f"NBDs: {nbds_str}")
else:
    o(f"NBD: {list(account_dates.values())[0].strftime('%Y%m%d')}")

o(f"Last BD: {last_bd_str}")
o(f"Last BD +1: {next_bd}")
o("")
o("-------------Actual Process (Bill Cycle Date)------------------------------")
o("1) Query")
o("select * from GPARAMS where name='SYSdateOverride';")
o("")
o("if not:")
o("insert into GPARAMS (name, \"TYPE\", start_dtm, string_value, integer_value) values('SYSdateOverride' ,'STRING',date '1990-01-01','ANY',null);")
o("COMMIT")
o("")
o("2) Billing account (SQL)")
o("SELECT * FROM account WHERE account_num in (")
for i, acc in enumerate(accounts):
    if i == len(accounts) - 1:
        o(f"'{acc}'")
    else:
        o(f"'{acc}',")
o(") order by NEXT_BILL_DTM asc;")
o("")
o("3) Login (PUTTY):")
o(UNIX_USER)
o(UNIX_PASSWORD)
o(f"dzdo -iu {infgca}")
o(UNIX_PASSWORD)
o("")
o("4) Folder creation & Today's date for IREP:")
o(f"cd {folder}")
o(f"mkdir {ticket}")
o("chmod 777 *")
o(f"cd {ticket}")
o(f"export GENEVA_FIXEDDATE='{today_str} 00000000'")
o("IREP")
o("")
o("5) Billing")

# Generate billing commands grouped by NBD group
bg_log_counter = 1

for nbd_date in sorted(nbd_groups.keys()):
    accts_in_group = nbd_groups[nbd_date]
    
    # Generate all billing dates for this NBD group
    current_date = nbd_date
    group_dates = []
    billing_count = 0
    
    while True:
        group_dates.append(current_date)
        billing_count += 1
        
        # Check stopping condition based on mode
        if billing_config['mode'] == 'count':
            if billing_count >= billing_config['value']:
                break
        else:  # mode == 'month_year'
            end_month = billing_config['month']
            end_year = billing_config['year']
            if current_date.year > end_year or (current_date.year == end_year and current_date.month >= end_month):
                break
        
        current_date = current_date + relativedelta(months=1)
    
    # Generate BG commands for each date in this group
    for date in group_dates:
        date_str = date.strftime("%Y%m%d")
        bg_args = " ".join(f"-a {a}" for a in accts_in_group)
        
        o(f"export GENEVA_FIXEDDATE='{date_str} 00000000'")
        o(f'BG -a "{bg_args}" >>BGLog{bg_log_counter if bg_log_counter > 1 else ""}')
        
        bg_log_counter += 1
    
    o("chmod 777 *")
    o("")

o("6) .xml Creation + BDW, MFM & Language")
o("ING >>ING")
o("BDW -a \"-formatterID 2 -bill\">>BDW")
o("BDW -m COMPLETE -a \"-bill -formatterID 2\">>BDW2")
o("MFM -a \"-plugInPath $INFINYS_ROOT/RB/lib/libGnvJIBP.so\">>MFM")
o("export NLS_LANG=AMERICAN_AMERICA.AL32UTF8")
o("GCIMFE >> GCIMFE.log")
o("export NLS_LANG=AMERICAN_AMERICA.WE8MSWIN1252")
o("chmod 777 *")
o(f"cd /t3appdata/{infgca}/RBInterface/Data/GCIMFE/Invoices//BILLDATA/THRESHOLD/")
o("")
o("7) Copying files to folder:")
o(f"REPLACE THIS MANUALLY: {today_str}_001")
o(f"cp GCITelco_THRESHOLD_{today_str}_001.xml {base_path}")
o(f"cp PROCESS.JOB {base_path}")
o(f"cd {base_path}")
o("chmod 777 *")
o("")
o("tail -n 1 PROCESS.JOB > PROCESS.tmp && mv PROCESS.tmp PROCESS.JOB && sed -i 's/_THRESHOLD_/_THRESHOLD_TEST_/g; s/PROC=PDFOnly2/PROC=UATPDFONLY/g' PROCESS.JOB")
o(f"mv GCITelco_THRESHOLD_{today_str}_001.xml GCITelco_THRESHOLD_TEST_{today_str}_001.xml")
o(f"zip GCITelco_THRESHOLD_TEST_{today_str}_001.zip GCITelco_THRESHOLD_TEST_{today_str}_001.xml PROCESS.JOB")
o("chmod 777 *")
o("")
o("--------------------------------------------------------------------------------------------------------")
o("")
o("Want to do Journaling?")
o("1) Here:")
o("cat > a.txt")
for acc in accounts:
    o(acc)
o("")
o("Ctrl D")
o("")
o(f"export GENEVA_FIXEDDATE='{next_bd} 00000000'")
o(f'FDE -m INITIALISE -a "-extractMode 1 -extractDat {last_bd_str}">>FDE_Actual_I01.log')
o(f'nohup FDE -m WORK -a "-extractMode 1 -extractDat {last_bd_str} -acctsFile a.txt">> FDE_Actual_W.log 2>&1 &')
o("")
o(f'FDE -m COMPLETE -a "-extractMode 1 -extractDat {last_bd_str}">>FDE_Actual_C.log')
o("chmod 777 *")
o("")

for d in sorted_bill_dates:
    o(f'IREP -a "-revenueMode ACTUAL -billDtm {d.strftime("%Y%m%d")}"')
o("")

o(f'FDE -m INITIALISE -a "-extractMode 4 -extractDat {last_bd_str}" >>FDE_IREP_I')
o(f'FDE -m WORK -a "-extractMode 4 -extractDat {last_bd_str} -acctsFile a.txt" >>FDE_IREP_W')
o("")
o(f'FDE -m COMPLETE -a "-extractMode 4 -extractDat {last_bd_str}" >> FDE_IREP_C')
o('nohup GLIFACE -a "-revenueMode ACTUAL -mode NORMAL">> GLIFACE_ACTUAL3.log 2>&1 &')
o("chmod 777 *")
o("")

# Generate RRDEP commands - grouped by date
counter = 1
extract_counter = 1

for bill_date in sorted_bill_dates:
    date_str = bill_date.strftime("%Y%m%d")
    
    # Find all accounts that have this billing date
    accts_for_date = []
    for account, dates in account_bill_dates.items():
        if bill_date in dates:
            accts_for_date.append(account)
    
    for acc in accts_for_date:
        o(f'RRDEP -a "-m Normal -type Revenue -extractDate {date_str} -accountNum {acc}" >ACT_N{counter}')
        counter += 1
    
    o(f'RRDEP -a "-m Extract -type Revenue -extractDate {date_str}" >>ACT_E{extract_counter:02d}')
    o(f'RRDEP -a "-m Extract -type BillEvent -extractDate {date_str}" >>RRDEP_BE{extract_counter:02d}')
    o("chmod 777 *")
    o("")
    
    extract_counter += 1

o("REMEMBER TO TAKE FILES NOW!!")
o(f"find /t3appdata/{infgca}/RBInterface/Data/RRDEP/RRFiles/ -name 'NC1*' -mmin -5 | zip -j {base_path}/RevRec_Files.zip -@")
o(f"find /t3appdata/{infgca}/RBInterface/Data/GLIFACE/Feed_files/processed -name 'NC1*' -mmin -5 | zip -j {base_path}/Oracle_Files.zip -@")
o("chmod 777 *")
o("")
o("WinSCP:")
o(f"/t3appdata/{infgca}/RBInterface/Data/RRDEP/RRFiles/")
o(f"/t3appdata/{infgca}/RBInterface/Data/GLIFACE/Feed_files/processed")
o("")
o("if manual:")
o(f"cd /t3appdata/{infgca}/RBInterface/Data/GLIFACE/Feed_files/processed")
o("chmod 777 *")
o(f"cd /t3appdata/{infgca}/RBInterface/Data/RRDEP/RRFiles/")
o("chmod 777 *")
o(f"cd {base_path}")
o("chmod 777 *")
o("")
o(f'FDE -m INITIALISE -a "-extractMode 8 -extractDat {last_bd_str}" >>FDE_RRACT_I')
o(f'FDE -m WORK -a "-extractMode 8 -extractDat {last_bd_str} -acctsFile a.txt" >>FDE_RRACT_W')
o("")
o(f'FDE -m COMPLETE -a "-extractMode 8 -extractDat {last_bd_str}" >> FDE_RRACT_C')
o('nohup GLIFACE -a "-revenueMode ACTUAL -mode NORMAL">> GLIFACE_ACTUAL4.log 2>&1 &')
o("chmod 777 *")
o("")
o("Refresh WINSCP & Take New Files:")
o("")
o("if manual:")
o(f"cd /t3appdata/{infgca}/RBInterface/Data/GLIFACE/Feed_files/processed")
o("chmod 777 *")
o(f"cd {base_path}")
o("chmod 777 *")
o("")
o(f"find /t3appdata/{infgca}/RBInterface/Data/GLIFACE/Feed_files/processed -name 'NC1*' -mmin -5 | zip -j Offset_Files.zip -@")
o("chmod 777 *")
o("")
o("FINACCTJOURNAL export:")
o("select * from finacctjournal where account_num in (")
for i, acc in enumerate(accounts):
    if i == len(accounts) - 1:
        o(f"'{acc}'")
    else:
        o(f"'{acc}',")
o(") and fde_extract_id in ('4400', '4401','4402');")
o("")
o(rf"C:\Users\you\Documents\{ticket}\FINACCTJOURNAL.xlsx")
o("")
o("zipzipzip")
o("")
o(rf"cat > NCP-{ticket}.txt")
o("")
o("DONE")

# Write file
with open(out_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"\n✅ Output saved to:\n{out_path}")
