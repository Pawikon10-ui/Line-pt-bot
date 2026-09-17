from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# โหลดคีย์จาก credentials.json
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
client = gspread.authorize(creds)

# ใช้ ID ของ Sheet PT_Clinic_Bookings ที่สร้างไว้
SPREADSHEET_ID = "1NuGHeurpnpXnEefOV1iA8K627biu8itiupyLl2I7cpE"
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

def add_test_booking():
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
    add_test_booking()
