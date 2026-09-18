from datetime import datetime
import os
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def add_test_booking():
    """Opt-in integration test; this function writes a real row when called."""
    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    spreadsheet_id = os.environ["GOOGLE_SPREADSHEET_ID"]
    creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(spreadsheet_id).sheet1
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    user_id = "U_TEST_001"
    name = "ทดสอบ ระบบจอง"
    phone = "081-999-8888"
    symptom = "ปวดคอบ่า Office Syndrome"
    appointment_time = "2026-09-22 13:00"
    status = "รอยืนยัน"
    note = "ทดสอบรันจากเครื่อง"

    row = [timestamp, user_id, name, phone, symptom, appointment_time, status, note]
    sheet.append_row(row)
    print(" บันทึกข้อมูลทดสอบลง Google Sheet สำเร็จแล้ว!")

if __name__ == "__main__":
    if os.getenv("RUN_SHEETS_INTEGRATION_TEST") == "1":
        add_test_booking()
    else:
        print("Skipped: set RUN_SHEETS_INTEGRATION_TEST=1 to write a test row.")
