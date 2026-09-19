from datetime import datetime
import json
import os
import re
import threading
import time
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from google import genai
from google.oauth2.service_account import Credentials
import gspread
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessageAction,
    MessagingApi,
    QuickReply,
    QuickReplyItem,
    ReplyMessageRequest,
    ShowLoadingAnimationRequest,
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent
import uvicorn

load_dotenv()

# --- 1. ข้อมูลการเชื่อมต่อ LINE & Gemini ---
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CONFIGURED = bool(CHANNEL_SECRET and CHANNEL_ACCESS_TOKEN)

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN or "")
handler = WebhookHandler(CHANNEL_SECRET or "")

if GEMINI_API_KEY:
  ai_client = genai.Client(api_key=GEMINI_API_KEY)
else:
  ai_client = None

# กำหนดบทบาทของ AI สำหรับคลินิกกายภาพบำบัด
SYSTEM_INSTRUCTION = """
คุณคือผู้ช่วยให้คำปรึกษาประจำ "คลินิกกายภาพบำบัด"
บุคลิก: สุภาพ อบอุ่น เป็นมิตร เชี่ยวชาญ ให้กำลังใจ และลงท้ายด้วย "ค่ะ / นะคะ" เสมอ

หน้าที่และข้อกำหนดสำคัญ:
1. ให้ความรู้และคำแนะนำเบื้องต้นเกี่ยวกับอาการปวดเมื่อย กล้ามเนื้อ กระดูก ข้อต่อ ท่าบริหาร และการปรับสรีระ (Ergonomics)
2. มีข้อจำกัดความรับผิดชอบ (Disclaimer) เสมอว่าเป็นการประเมินเบื้องต้น ไม่สามารถทดแทนการตรวจร่างกายโดยตรง
3. ห้ามสั่งจ่ายยา หรือวินิจฉัยโรคขั้นสุดท้าย
4. หากพบอาการสัญญาณอันตราย (Red Flags เช่น กลั้นปัสสาวะ/อุจจาระไม่ได้, แขนขาอ่อนแรงฉับพลัน, ชาเป็นบริเวณกว้าง, ปวดรุนแรงหลังอุบัติเหตุ) ให้แนะนำพบแพทย์ทันที
5. ตอบให้กระชับ ชัดเจน ตรงประเด็น ใช้ bullet point สั้นๆ
6. ในตอนท้ายของคำตอบ ให้เชิญชวนอย่างนุ่มนวลว่า "หากต้องการตรวจประเมินร่างกายอย่างละเอียดกับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ"
7. หากคนไข้สอบถามเกี่ยวกับการเปลี่ยนชื่อ เปลี่ยนเบอร์โทร หรือแก้ไขข้อมูลส่วนตัว:
   - ห้ามขอชื่อเดิม และห้ามบอกให้รอเจ้าหน้าที่เด็ดขาด
   - ให้แจ้งอย่างชัดเจนว่า "สามารถเปลี่ยนได้ทันทีเลยค่ะ 😊 เพียงพิมพ์ 'ชื่อ-นามสกุลใหม่' ส่งมาในแชตนี้ได้เลยนะคะ (ไม่ต้องพิมพ์ชื่อเดิมค่ะ) หรือหากต้องการเปลี่ยนเบอร์ด้วย ก็สามารถพิมพ์เบอร์ใหม่ส่งมาได้เลยค่ะ ระบบจะบันทึกอัปเดตให้อัตโนมัติทันทีค่ะ"
"""


# --- Gemini resilient generation ---
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
    if m.strip() and m.strip() != GEMINI_MODEL
]
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", "4"))
GEMINI_RETRY_BASE_SECONDS = float(os.getenv("GEMINI_RETRY_BASE_SECONDS", "1.5"))


def _is_retryable_gemini_error(err: Exception) -> bool:
  """Retry transient Gemini availability/rate-limit/server errors."""
  msg = str(err).upper()
  return any(token in msg for token in (
      "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
      "500", "INTERNAL", "502", "BAD GATEWAY", "504", "DEADLINE",
  ))


def generate_gemini_response(user_text: str) -> Optional[str]:
  """Generate a Gemini answer with exponential backoff and optional fallback models."""
  if not ai_client:
    return None

  models = [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS

  for model_name in models:
    for attempt in range(GEMINI_MAX_RETRIES + 1):
      try:
        response = ai_client.models.generate_content(
            model=model_name,
            contents=user_text,
            config={
                "system_instruction": SYSTEM_INSTRUCTION,
                "max_output_tokens": 1024,
            },
        )
        answer = get_visible_ai_text(response)
        if answer:
          return answer

        print(f"Gemini model {model_name} returned no visible text.")
        break

      except Exception as err:
        retryable = _is_retryable_gemini_error(err)
        if not retryable or attempt >= GEMINI_MAX_RETRIES:
          print(f"Gemini model {model_name} failed (attempt {attempt + 1}): {err}")
          break

        # Exponential backoff: 1.5, 3, 6, 12 seconds by default.
        wait_seconds = GEMINI_RETRY_BASE_SECONDS * (2 ** attempt)
        print(
            f"Gemini model {model_name} temporarily unavailable "
            f"(attempt {attempt + 1}/{GEMINI_MAX_RETRIES + 1}). "
            f"Retrying in {wait_seconds:.1f}s: {err}"
        )
        time.sleep(wait_seconds)

  return None

# --- 2. ข้อมูลการเชื่อมต่อ Google Sheets ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
sheet = None
sheet_lock = threading.Lock()

# ตรวจสอบหาไฟล์ credentials.json ทั้งในเครื่อง Mac และบน Render (/etc/secrets/credentials.json)
creds_path = None
for path in filter(None, [
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    "credentials.json",
    "/etc/secrets/credentials.json",
    "/opt/render/project/src/credentials.json",
]):
  if os.path.exists(path):
    creds_path = path
    break

if not SPREADSHEET_ID:
  print("Warning: GOOGLE_SPREADSHEET_ID is not configured!")
elif os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
  try:
    creds = Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    print("Google Sheets connected successfully from GOOGLE_SERVICE_ACCOUNT_JSON!")
  except Exception as e:
    print(f"Google Sheets connection error: {e}")
elif creds_path:
  try:
    creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
    print(f"Google Sheets connected successfully from {creds_path}!")
  except Exception as e:
    print(f"Google Sheets connection error: {e}")
else:
  print("Warning: credentials.json not found in any expected paths!")

user_sessions = {}
patient_profile_cache = {}  # {user_id: {"data": profile_dict, "time": float}}
SESSION_TTL_SECONDS = 60 * 60

CANCEL_KEYWORDS = [
    "ยกเลิก",
    "ไม่จอง",
    "ไม่นัด",
    "หยุดก่อน",
    "ออก",
    "ยกเลิกนัด",
    "ยกเลิกการจอง",
]

EDIT_KEYWORDS = [
    "แก้ไขชื่อ",
    "เปลี่ยนชื่อ",
    "แก้ชื่อ",
    "ขอเปลี่ยนชื่อ",
    "อยากเปลี่ยนชื่อ",
    "เปลี่ยนชื่อยังไง",
    "แก้ไขเบอร์",
    "เปลี่ยนเบอร์",
    "แก้เบอร์",
    "ขอเปลี่ยนเบอร์",
    "อยากเปลี่ยนเบอร์",
    "เปลี่ยนเบอร์ยังไง",
    "แก้ไขข้อมูล",
    "แก้ข้อมูล",
    "เปลี่ยนข้อมูล",
    "อัปเดตข้อมูล",
    "อัปเดตเบอร์",
    "อัปเดตชื่อ",
    "อัพเดทข้อมูล",
    "อัพเดทเบอร์",
    "อัพเดทชื่อ",
    "เปลี่ยนชื่อได้ไหม",
    "เปลี่ยนเบอร์ได้ไหม",
]

INVALID_SYMPTOMS = [
    "ยกเลิก",
    "ไม่จอง",
    "รอยืนยัน",
    "คนไข้ผ่าน line",
    "ทดสอบ",
    "สวัสดี",
    "เปลี่ยนชื่อ",
    "แก้ชื่อ",
    "แก้เบอร์",
    "ลงทะเบียนข้อมูล",
    "บันทึกประวัติ",
    "-",
    "ไม่มี",
    "แม่ง",
    "รวน",
    "ได้ไหม",
]

MENU_KEYWORDS = [
    "แจ้งอาการใหม่",
    "นัดรักษาอาการเดิม",
    "แก้ไขชื่อ/เบอร์โทร",
    "อาการใหม่",
    "อาการเดิม",
    "รักษาต่อเนื่อง",
    "จองคิว",
    "นัดหมาย",
]


def clean_text(text: str) -> str:
  """ตัดคำลงท้ายออกจากท้ายประโยค"""
  pattern = (
      r"(\s*(นะคะ|นะครับ|ครับผม|ค่ะ|ครับ|ค่า|ค้า|ค๊า|คับ|คั้บ|งับ|จ้า|จ้ะ|ฮะ|นะ))+\s*$"
  )
  return re.sub(pattern, "", text.strip()).strip()


def clean_symptom_text(symptom: str) -> str:
  """ล้างคำซ้ำซ้อนในอาการ เช่น (รักษาต่อเนื่อง) หลายรอบ"""
  if not symptom:
    return "ตรวจประเมินร่างกาย"
  cleaned = re.sub(r"(\s*\(รักษาต่อเนื่อง\))+", "", symptom).strip()
  return cleaned if cleaned else "ตรวจประเมินร่างกาย"


def get_visible_ai_text(response) -> Optional[str]:
  """คืนเฉพาะข้อความคำตอบ ไม่รวม thought/thought signature ของ Gemini 3."""
  for candidate in getattr(response, "candidates", None) or []:
    content = getattr(candidate, "content", None)
    for part in getattr(content, "parts", None) or []:
      part_text = getattr(part, "text", None)
      if part_text and not getattr(part, "thought", False):
        return part_text.strip()
  return None


def extract_contact_info(
    text: str, default_name: str = None, default_phone: str = None
):
  """สกัดชื่อและเบอร์โทรศัพท์จากข้อความอย่างยืดหยุ่น"""
  phone_match = re.search(r"0[689]\d{1}[- ]?\d{3}[- ]?\d{4}|0\d{9}", text)
  phone = None
  name = None

  if phone_match:
    raw_phone = phone_match.group()
    phone = raw_phone.replace("-", "").replace(" ", "")
    raw_name = text.replace(raw_phone, "").strip()
  else:
    raw_name = text.strip()

  cleaned_name = re.sub(
      r"^(ชื่อ|คุณ|ติดต่อ|แก้ไขเป็น|เปลี่ยนเป็น|เปลี่ยนชื่อเป็น|แก้ชื่อเป็น|แก้เบอร์เป็น|เปลี่ยนเบอร์เป็น|ชื่อใหม่|เบอร์ใหม่|เบอร์ใหม่เป็น|เปลี่ยนเบอร์|แก้เบอร์|แก้ไขเบอร์|เปลี่ยนชื่อ|แก้ชื่อ|แก้ไขชื่อ|เบอร์โทร|เบอร์โทรใหม่|โทร|เป็น|คือ)\s*[:\s]*",
      "",
      raw_name,
  ).strip()
  cleaned_name = clean_text(cleaned_name)
  cleaned_name = re.sub(
      r"^(ได้ไหม|ยังไง|หน่อย|ครับ|ค่ะ|คะ|นะ)+\s*$", "", cleaned_name
  ).strip()
  cleaned_name = re.sub(
      r"^(เป็น|คือ|ชื่อ|เบอร์)\s*", "", cleaned_name
  ).strip()

  # ตรวจสอบว่า cleaned_name ต้องไม่ใช่คำสั่ง คำในเมนู คำยกเลิก หรือคำทั่วไป
  is_invalid_name = (
      not cleaned_name
      or len(cleaned_name) < 2
      or cleaned_name in ["เป็น", "คือ", "เบอร์", "ชื่อ", "แก้ไข", "เปลี่ยน", "ข้อมูล", "ใหม่"]
      or any(
          k in cleaned_name
          for k in EDIT_KEYWORDS
          + CANCEL_KEYWORDS
          + MENU_KEYWORDS
          + ["แก้ไข", "เปลี่ยน", "เบอร์", "ชื่อ", "ข้อมูล"]
      )
  )

  if not is_invalid_name:
    name = cleaned_name
  else:
    name = default_name

  if not phone:
    phone = default_phone

  return name, phone


def get_patient_profile(user_id: str):
  """ค้นหาประวัติคนไข้เดิมจาก Cache หรือ Google Sheet โดยกรองอาการที่เป็นข้อความขยะออก"""
  now = time.time()
  if user_id in patient_profile_cache:
    cached = patient_profile_cache[user_id]
    if now - cached["time"] < 1800:  # แคชไว้ 30 นาทีเพื่อความรวดเร็ว
      return cached["data"]

  if not sheet:
    return None
  try:
    with sheet_lock:
      records = sheet.get_all_values()
    # หาแถวล่าสุดที่มีอาการจริง (ไม่ใช่อาการขยะ เช่น ยกเลิก หรือ สวัสดี)
    for row in reversed(records[1:]):
      if len(row) >= 5:
        r_time, r_uid, r_name, r_phone, r_symptom = row[:5]
        if r_uid == user_id:
          if (
              r_name
              and r_name != "คนไข้ผ่าน LINE"
              and r_phone
              and r_phone != "-"
          ):
            sym_clean = clean_symptom_text(r_symptom)
            if any(inv in sym_clean.lower() for inv in INVALID_SYMPTOMS):
              continue  # ข้ามแถวที่อาการไม่ใช่โรคหรืออาการจริง

            prof = {
                "name": r_name,
                "phone": r_phone,
                "last_symptom": sym_clean,
            }
            patient_profile_cache[user_id] = {"data": prof, "time": now}
            return prof

    # หากไม่พบอาการจริง ให้ดึงชื่อและเบอร์มา แล้วใช้อาการมาตรฐาน
    for row in reversed(records[1:]):
      if len(row) >= 5 and row[1] == user_id:
        r_name, r_phone = row[2], row[3]
        if r_name and r_name != "คนไข้ผ่าน LINE" and r_phone and r_phone != "-":
          prof = {
              "name": r_name,
              "phone": r_phone,
              "last_symptom": "ตรวจประเมินร่างกาย",
          }
          patient_profile_cache[user_id] = {"data": prof, "time": now}
          return prof
  except Exception as e:
    print(f"Error fetching profile: {e}")
  return None


def _sync_profile_to_sheets(user_id: str, new_name: str, new_phone: str) -> bool:
  """บันทึกข้อมูลคนไข้ลง Google Sheets และรายงานผลลัพธ์ให้ผู้เรียกใช้"""
  if not sheet:
    return False
  try:
    # การอ่านและเขียนต้องอยู่ใน lock เดียวกัน เพื่อไม่ให้การจองพร้อมกันเขียนทับแถว
    with sheet_lock:
      records = sheet.get_all_values()
      target_row_idx = None
      for idx, row in enumerate(records, start=1):
        if idx > 1 and len(row) >= 2 and row[1] == user_id:
          target_row_idx = idx

      if target_row_idx:
        sheet.update(
            range_name=f"C{target_row_idx}:D{target_row_idx}",
            values=[[new_name, new_phone]],
        )
      else:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp, user_id, new_name, new_phone, "-", "-", "บันทึกประวัติ",
            "อัปเดตข้อมูลผ่าน LINE",
        ]
        sheet.append_row(row)
    return True
  except Exception as e:
    print(f"Error updating patient profile in Google Sheets: {e}")
    return False


def update_patient_profile(user_id: str, new_name: str, new_phone: str) -> bool:
  """บันทึกข้อมูลก่อน แล้วค่อยอัปเดต cache เพื่อไม่รายงานความสำเร็จเกินจริง"""
  if not _sync_profile_to_sheets(user_id, new_name, new_phone):
    return False
  now = time.time()
  old_prof = patient_profile_cache.get(user_id, {}).get("data", {})
  last_symptom = old_prof.get("last_symptom", "ตรวจประเมินร่างกาย")
  patient_profile_cache[user_id] = {
      "data": {
          "name": new_name,
          "phone": new_phone,
          "last_symptom": last_symptom,
      },
      "time": now,
  }
  return True


def _sync_booking_to_sheets(row: list) -> bool:
  """บันทึกคำขอนัดหมายลง Google Sheets แบบตรวจสอบผลลัพธ์"""
  if not sheet:
    return False
  try:
    with sheet_lock:
      sheet.append_row(row)
    return True
  except Exception as e:
    print(f"Error updating row to Google Sheets: {e}")
    return False


app = FastAPI()


@app.api_route("/", methods=["GET", "HEAD", "POST"])
@app.api_route("/ping", methods=["GET", "HEAD", "POST"])
async def ping():
  """Healthcheck endpoint สำหรับ UptimeRobot หรือ Cron Ping ป้องกัน Render หลับ"""
  return {"status": "ok", "message": "LINE PT Bot is awake!"}


@app.post("/callback")
async def callback(
    request: Request, x_line_signature: str = Header(None, alias="x-line-signature")
):
  if not LINE_CONFIGURED:
    raise HTTPException(status_code=503, detail="LINE credentials are not configured")
  body = (await request.body()).decode("utf-8")
  if not x_line_signature:
    raise HTTPException(status_code=400, detail="Missing signature")
  try:
    handler.handle(body, x_line_signature)
  except InvalidSignatureError:
    raise HTTPException(status_code=400, detail="Invalid signature")
  return "OK"


# --- ระบบต้อนรับเมื่อมีคนกดเพิ่มเพื่อน (Follow Event) ---
@handler.add(FollowEvent)
def handle_follow(event):
  welcome_text = (
      "สวัสดีค่ะ ยินดีต้อนรับสู่คลินิกกายภาพบำบัดนะคะ 🏥✨\n\n"
      "คุณสามารถ:\n"
      "💬 พิมพ์ปรึกษาอาการปวดเมื่อย ท่าบริหาร หรือข้อสงสัยทางกายภาพบำบัดได้เลยค่ะ\n"
      "📅 พิมพ์ 'จองคิว' เพื่อทำการนัดหมายตรวจประเมินกับนักกายภาพบำบัดค่ะ\n"
      "⚙️ พิมพ์ 'แก้ไขข้อมูล' หรือ 'เปลี่ยนชื่อ' เพื่ออัปเดตข้อมูลส่วนตัวได้ตลอดเวลาค่ะ\n\n"
      "ยินดีดูแลสุขภาพร่างกายของคุณนะคะ 😊"
  )
  with ApiClient(configuration) as api_client:
    line_bot_api = MessagingApi(api_client)
    line_bot_api.reply_message(
        ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=welcome_text)],
        )
    )


# --- ระบบจัดการข้อความ ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
  user_text = event.message.text.strip()
  user_id = event.source.user_id

  with ApiClient(configuration) as api_client:
    line_bot_api = MessagingApi(api_client)

    # ส่ง Loading animation (จุดสามจุดกำลังพิมพ์...) ใน Background Thread
    try:
      threading.Thread(
          target=line_bot_api.show_loading_animation,
          args=(ShowLoadingAnimationRequest(chat_id=user_id, loading_seconds=15),),
          daemon=True,
      ).start()
    except Exception:
      pass

    session = user_sessions.get(user_id, {})
    now = time.time()
    if session and now - session.get("_updated_at", now) > SESSION_TTL_SECONDS:
      user_sessions.pop(user_id, None)
      session = {}
    session["_updated_at"] = now
    current_step = session.get("step")
    reply_msg = None

    # --- 1. ตรวจจับคำสั่งยกเลิก (Cancel) สากลในทุกขั้นตอน ---
    if any(k in user_text for k in CANCEL_KEYWORDS) and current_step is not None:
      user_sessions.pop(user_id, None)
      reply_msg = TextMessage(
          text=(
              "รับทราบการยกเลิกเรียบร้อยค่ะ 😊\n\n"
              "หากต้องการปรึกษาอาการ ปรับสรีระ หรือนัดหมายตรวจร่างกายใหม่ "
              "สามารถพิมพ์สอบถาม หรือพิมพ์ 'จองคิว' ได้ตลอดเวลานะคะ 🏥"
          )
      )

    # --- 2. ตรวจจับคำสั่งแก้ไขชื่อ/เบอร์โทร (Edit Contact) สากลในทุกขั้นตอน ---
    elif any(k in user_text for k in EDIT_KEYWORDS) and current_step not in [
        "WAITING_EDIT_CONTACT"
    ]:
      profile = get_patient_profile(user_id)
      curr_name = profile["name"] if profile else session.get("name")
      curr_phone = profile["phone"] if profile else session.get("phone")

      # ตรวจสอบว่าผู้ใช้พิมพ์ชื่อใหม่หรือเบอร์ใหม่มาในข้อความนี้เลยหรือไม่
      new_name, new_phone = extract_contact_info(
          user_text, curr_name, curr_phone
      )
      if (
          curr_name
          and (new_name != curr_name or new_phone != curr_phone)
          and new_name
          and new_phone
      ):
        # มีการระบุชื่อใหม่หรือเบอร์ใหม่มาพร้อมคำสั่ง เช่น 'เปลี่ยนชื่อเป็น ปวีณ์กร การเร็ว'
        update_patient_profile(user_id, new_name, new_phone)
        session["name"] = new_name
        session["phone"] = new_phone
        user_sessions[user_id] = session

        if session.get("symptom"):
          session["step"] = "WAITING_DATETIME"
          reply_msg = TextMessage(
              text=(
                  f"✅ อัปเดตข้อมูลเป็น คุณ {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  "สะดวกเข้ามาตรวจประเมินวันและเวลาใดดีคะ?\n"
                  "(ตัวอย่าง: วันเสาร์นี้ 14:00 น., 22 ก.ย. ช่วงบ่าย)"
              )
          )
        elif session.get("last_symptom"):
          session["step"] = "RETURNING_CHOICE"
          reply_msg = TextMessage(
              text=(
                  f"✅ อัปเดตข้อมูลเป็น คุณ {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  f"ต้องการนัดหมายรักษาอาการเดิม ({session.get('last_symptom')})\nหรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
              ),
              quick_reply=QuickReply(
                  items=[
                      QuickReplyItem(
                          action=MessageAction(
                              label="นัดรักษาอาการเดิม",
                              text="นัดรักษาอาการเดิม",
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แจ้งอาการใหม่", text="แจ้งอาการใหม่"
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แก้ไขชื่อ/เบอร์โทร",
                              text="แก้ไขชื่อ/เบอร์โทร",
                          )
                      ),
                  ]
              ),
          )
        else:
          user_sessions.pop(user_id, None)
          reply_msg = TextMessage(
              text=(
                  f"✅ อัปเดตข้อมูลเป็น คุณ {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  "หากต้องการนัดหมายตรวจรักษากับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ 🏥"
              )
          )
      else:
        # ยังไม่ได้ระบุชื่อหรือเบอร์ใหม่มา ให้แจ้งวิธีพิมพ์ที่เข้าใจง่าย
        user_sessions[user_id] = {
            "step": "WAITING_EDIT_CONTACT",
            "name": curr_name,
            "phone": curr_phone,
            "last_symptom": (
                profile["last_symptom"]
                if profile
                else session.get("last_symptom")
            ),
            "symptom": session.get("symptom"),
        }

        if curr_name and curr_phone:
          reply_msg = TextMessage(
              text=(
                  "สามารถเปลี่ยนชื่อหรือเบอร์โทรได้ทันทีเลยค่ะ 😊\n"
                  "*(ไม่ต้องพิมพ์ชื่อเดิมนะคะ)*\n\n"
                  "📌 ข้อมูลปัจจุบันของคุณในระบบ:\n"
                  f"👤 ชื่อ: {curr_name}\n"
                  f"📞 เบอร์โทร: {curr_phone}\n\n"
                  "เพียงพิมพ์ข้อมูลใหม่ส่งมาได้เลยค่ะ:\n"
                  "👉 พิมพ์เฉพาะชื่อ-นามสกุลใหม่ (เช่น ปวีณ์กร การเร็ว)\n"
                  "👉 หรือพิมพ์เฉพาะเบอร์โทรใหม่ 10 หลัก (เช่น 0891234567)\n"
                  "👉 หรือพิมพ์ทั้งชื่อและเบอร์ใหม่พร้อมกันได้เลยค่ะ ✍️"
              )
          )
        else:
          reply_msg = TextMessage(
              text=(
                  "ยังไม่พบข้อมูลประวัติเดิมในระบบค่ะ 🏥\n\n"
                  "คุณสามารถพิมพ์ 'ชื่อ-นามสกุล' หรือ 'ชื่อพร้อมเบอร์โทร' เพื่อบันทึกข้อมูลได้เลยค่ะ ✍️\n"
                  "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
              )
          )

    # --- 3. ขั้นตอนรับข้อมูลชื่อ/เบอร์ใหม่ ---
    elif current_step == "WAITING_EDIT_CONTACT":
      curr_name = session.get("name")
      curr_phone = session.get("phone")
      new_name, new_phone = extract_contact_info(
          user_text, curr_name, curr_phone
      )

      if not new_phone:
        reply_msg = TextMessage(
            text=(
                "ขออภัยค่ะ ระบบไม่พบเบอร์โทรศัพท์ 10 หลัก\n"
                "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่อีกครั้งนะคะ\n"
                "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
            )
        )
      elif not new_name:
        reply_msg = TextMessage(
            text=(
                "ขออภัยค่ะ ไม่พบชื่อ-นามสกุล\n"
                "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่อีกครั้งนะคะ\n"
                "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
            )
        )
      else:
        session["name"] = new_name
        session["phone"] = new_phone
        update_patient_profile(user_id, new_name, new_phone)

        # หากกำลังอยู่ในระหว่างการจองคิว ให้ดำเนินขั้นตอนต่อไป
        if session.get("symptom"):
          session["step"] = "WAITING_DATETIME"
          user_sessions[user_id] = session
          reply_msg = TextMessage(
              text=(
                  "✅ อัปเดตข้อมูลเป็น คุณ"
                  f" {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  "สะดวกเข้ามาตรวจประเมินวันและเวลาใดดีคะ?\n"
                  "(ตัวอย่าง: วันเสาร์นี้ 14:00 น., 22 ก.ย. ช่วงบ่าย)"
              )
          )
        elif session.get("last_symptom"):
          session["step"] = "RETURNING_CHOICE"
          user_sessions[user_id] = session
          reply_msg = TextMessage(
              text=(
                  "✅ อัปเดตข้อมูลเป็น คุณ"
                  f" {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  "ต้องการนัดหมายรักษาอาการเดิม"
                  f" ({session.get('last_symptom')})\nหรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
              ),
              quick_reply=QuickReply(
                  items=[
                      QuickReplyItem(
                          action=MessageAction(
                              label="นัดรักษาอาการเดิม",
                              text="นัดรักษาอาการเดิม",
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แจ้งอาการใหม่", text="แจ้งอาการใหม่"
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แก้ไขชื่อ/เบอร์โทร",
                              text="แก้ไขชื่อ/เบอร์โทร",
                          )
                      ),
                  ]
              ),
          )
        else:
          user_sessions.pop(user_id, None)
          reply_msg = TextMessage(
              text=(
                  "✅ อัปเดตข้อมูลเป็น คุณ"
                  f" {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  "หากต้องการนัดหมายตรวจรักษากับนักกายภาพบำบัด"
                  " สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ 🏥"
              )
          )

    # --- 4. ตรวจจับคำว่า 'จองคิว' หรือ 'นัดหมาย' ---
    elif any(keyword in user_text for keyword in ["จองคิว", "นัดหมาย", "ขอนัด"]):
      profile = get_patient_profile(user_id)

      if profile:
        user_sessions[user_id] = {
            "step": "RETURNING_CHOICE",
            "name": profile["name"],
            "phone": profile["phone"],
            "last_symptom": profile["last_symptom"],
        }
        reply_msg = TextMessage(
            text=(
                f"ยินดีต้อนรับกลับค่ะ คุณ {profile['name']} 🏥\n"
                f"(เบอร์ติดต่อ: {profile['phone']})\n\n"
                f"ต้องการนัดหมายรักษาอาการเดิม ({profile['last_symptom']})\n"
                "หรือมีอาการใหม่แจ้งเพิ่มเติมคะ?\n\n"
                "💡 หากต้องการเปลี่ยนชื่อหรือเบอร์โทร สามารถพิมพ์ชื่อ-นามสกุลใหม่ หรือกดปุ่มด้านล่างได้เลยนะคะ"
            ),
            quick_reply=QuickReply(
                items=[
                    QuickReplyItem(
                        action=MessageAction(
                            label="นัดรักษาอาการเดิม", text="นัดรักษาอาการเดิม"
                        )
                    ),
                    QuickReplyItem(
                        action=MessageAction(
                            label="แจ้งอาการใหม่", text="แจ้งอาการใหม่"
                        )
                    ),
                    QuickReplyItem(
                        action=MessageAction(
                            label="แก้ไขชื่อ/เบอร์โทร",
                            text="แก้ไขชื่อ/เบอร์โทร",
                        )
                    ),
                ]
            ),
        )
      else:
        user_sessions[user_id] = {"step": "WAITING_SYMPTOM", "_updated_at": now}
        reply_msg = TextMessage(
            text=(
                "ยินดีต้อนรับสู่คลินิกกายภาพบำบัดค่ะ 🏥\n\n"
                "1/3 กรุณาพิมพ์ระบุอาการ หรือบริเวณที่มีปัญหาได้เลยค่ะ\n"
                "(เช่น ปวดสะบัก, ไหล่ติด, นิ้วล็อก, หมอนรองกระดูกทับเส้น เป็นต้น)"
            )
        )

    # --- 5. กรณีคนไข้เดิมเลือกตัวเลือก (RETURNING_CHOICE) ---
    elif current_step == "RETURNING_CHOICE":
      # 5.1 นัดรักษาอาการเดิม
      if (
          "อาการเดิม" in user_text
          or "รักษาต่อเนื่อง" in user_text
          or "เดิม" in user_text
      ):
        session["step"] = "WAITING_DATETIME"
        session["symptom"] = f"{session.get('last_symptom')} (รักษาต่อเนื่อง)"
        user_sessions[user_id] = session

        reply_msg = TextMessage(
            text=(
                f"รับทราบค่ะคุณ {session.get('name')}\n"
                f"นัดหมายตรวจรักษา: {session['symptom']}\n\n"
                "สะดวกเข้ามาตรวจวันและเวลาใดดีคะ?\n"
                "(ตัวอย่าง: พรุ่งนี้ 14:00 น., วันเสาร์ช่วงเช้า)"
            )
        )

      # 5.2 แจ้งอาการใหม่
      elif any(k in user_text for k in ["อาการใหม่", "ใหม่"]):
        session["step"] = "WAITING_NEW_SYMPTOM"
        user_sessions[user_id] = session
        reply_msg = TextMessage(
            text="กรุณาพิมพ์ระบุอาการใหม่ที่ต้องการปรึกษาได้เลยค่ะ:"
        )

      # 5.3 เลือกแก้ไขข้อมูลส่วนตัว หรือพิมพ์เบอร์ใหม่/ชื่อใหม่
      elif any(k in user_text for k in EDIT_KEYWORDS + ["แก้ไข", "เปลี่ยน", "เบอร์", "ชื่อ", "ข้อมูล"]) or re.search(r"0[689]\d{1}[- ]?\d{3}[- ]?\d{4}|0\d{9}", user_text):
        new_name, new_phone = extract_contact_info(
            user_text, session.get("name"), session.get("phone")
        )
        if (
            session.get("name")
            and (
                new_name != session.get("name")
                or new_phone != session.get("phone")
            )
            and new_name
            and new_phone
        ):
          session["name"] = new_name
          session["phone"] = new_phone
          update_patient_profile(user_id, new_name, new_phone)
          reply_msg = TextMessage(
              text=(
                  f"✅ อัปเดตข้อมูลเป็น คุณ {new_name} (เบอร์: {new_phone}) เรียบร้อยแล้วค่ะ! ✨\n\n"
                  f"ต้องการนัดหมายรักษาอาการเดิม ({session.get('last_symptom')})\n"
                  "หรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
              ),
              quick_reply=QuickReply(
                  items=[
                      QuickReplyItem(
                          action=MessageAction(
                              label="นัดรักษาอาการเดิม",
                              text="นัดรักษาอาการเดิม",
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แจ้งอาการใหม่", text="แจ้งอาการใหม่"
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แก้ไขชื่อ/เบอร์โทร",
                              text="แก้ไขชื่อ/เบอร์โทร",
                          )
                      ),
                  ]
              ),
          )
        else:
          session["step"] = "WAITING_EDIT_CONTACT"
          user_sessions[user_id] = session
          reply_msg = TextMessage(
              text=(
                  "สามารถเปลี่ยนชื่อหรือเบอร์โทรได้ทันทีเลยค่ะ 😊\n"
                  "*(ไม่ต้องพิมพ์ชื่อเดิมนะคะ)*\n\n"
                  f"📌 ข้อมูลปัจจุบันของคุณ: คุณ {session.get('name')} (เบอร์: {session.get('phone')})\n\n"
                  "เพียงพิมพ์ข้อมูลใหม่ส่งมาได้เลยค่ะ:\n"
                  "👉 พิมพ์เฉพาะชื่อ-นามสกุลใหม่ (เช่น ปวีณ์กร การเร็ว)\n"
                  "👉 หรือพิมพ์เฉพาะเบอร์โทรใหม่ (เช่น 0891234567)\n"
                  "👉 หรือพิมพ์ทั้งชื่อและเบอร์ใหม่พร้อมกันได้เลยค่ะ ✍️"
              )
          )

      # 5.4 พิมพ์ชื่ออาการใหม่มาเลยโดยตรง (เช่น ปวดสะบัก, ไหล่ติด)
      else:
        clean_symptom = clean_text(user_text)
        if len(clean_symptom) >= 2 and not any(
            inv in clean_symptom.lower() for inv in INVALID_SYMPTOMS
        ):
          session["symptom"] = clean_symptom
          session["step"] = "WAITING_DATETIME"
          user_sessions[user_id] = session
          reply_msg = TextMessage(
              text=(
                  f"รับทราบอาการใหม่: '{clean_symptom}' ค่ะ\n\n"
                  "สะดวกเข้ามาตรวจวันและเวลาใดดีคะ? (เช่น พรุ่งนี้บ่ายสอง,"
                  " วันอาทิตย์)"
              )
          )
        else:
          ans = generate_gemini_response(user_text)
          if not ans:
            ans = "กรุณาเลือกตัวเลือกที่ต้องการ หรือพิมพ์ระบุอาการใหม่ได้เลยนะคะ"

          reply_msg = TextMessage(
              text=ans,
              quick_reply=QuickReply(
                  items=[
                      QuickReplyItem(
                          action=MessageAction(
                              label="นัดรักษาอาการเดิม",
                              text="นัดรักษาอาการเดิม",
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แจ้งอาการใหม่", text="แจ้งอาการใหม่"
                          )
                      ),
                      QuickReplyItem(
                          action=MessageAction(
                              label="แก้ไขชื่อ/เบอร์โทร",
                              text="แก้ไขชื่อ/เบอร์โทร",
                          )
                      ),
                  ]
              ),
          )

    # --- 6. คนไข้เดิมพิมพ์อาการใหม่ ---
    elif current_step == "WAITING_NEW_SYMPTOM":
      clean_symptom = clean_text(user_text)
      if not clean_symptom or any(inv in clean_symptom.lower() for inv in INVALID_SYMPTOMS):
        reply_msg = TextMessage(text="กรุณาระบุอาการหรือบริเวณที่มีปัญหาเพิ่มเติมได้เลยค่ะ")
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg])
        )
        return
      session["symptom"] = clean_symptom
      session["step"] = "WAITING_DATETIME"
      user_sessions[user_id] = session

      reply_msg = TextMessage(
          text=(
              f"รับทราบอาการใหม่: '{clean_symptom}' ค่ะ\n\n"
              "สะดวกเข้ามาตรวจวันและเวลาใดดีคะ? (เช่น พรุ่งนี้บ่ายสอง,"
              " วันอาทิตย์)"
          )
      )

    # --- 7. สำหรับคนไข้ใหม่: รอรับอาการ ---
    elif current_step == "WAITING_SYMPTOM":
      clean_symptom = clean_text(user_text)
      if not clean_symptom or any(inv in clean_symptom.lower() for inv in INVALID_SYMPTOMS):
        reply_msg = TextMessage(text="กรุณาระบุอาการหรือบริเวณที่มีปัญหาเพิ่มเติมได้เลยค่ะ")
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg])
        )
        return
      user_sessions[user_id] = {
          "step": "WAITING_CONTACT",
          "symptom": clean_symptom,
          "_updated_at": now,
      }
      reply_msg = TextMessage(
          text=(
              f"รับทราบอาการ:\n'{clean_symptom}'\n\n"
              "2/3 กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' สำหรับติดต่อค่ะ\n"
              "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
          )
      )

    # --- 8. สำหรับคนไข้ใหม่: รอรับชื่อและเบอร์ ---
    elif current_step == "WAITING_CONTACT":
      name, phone = extract_contact_info(user_text)
      if phone and name and name != "คนไข้ผ่าน LINE":
        session["name"] = name
        session["phone"] = phone
        session["step"] = "WAITING_DATETIME"
        user_sessions[user_id] = session

        reply_msg = TextMessage(
            text=(
                f"ยินดีค่ะ คุณ {name} (เบอร์ติดต่อ: {phone})\n\n"
                "3/3 ขั้นตอนสุดท้าย: สะดวกเข้ามาตรวจประเมินวันและเวลาใดดีคะ?\n"
                "(ตัวอย่าง: วันเสาร์นี้ 14:00 น., 22 ก.ย. ช่วงบ่าย)\n\n"
                "(หากต้องการแก้ไขชื่อหรือเบอร์ สามารถพิมพ์ 'แก้ไขข้อมูล' ได้ค่ะ)"
            )
        )
      else:
        reply_msg = TextMessage(
            text=(
                "ขออภัยค่ะ ระบบไม่พบเบอร์โทรศัพท์ 10 หลักที่ถูกต้อง\n"
                "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' สำหรับติดต่ออีกครั้งนะคะ\n"
                "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
            )
        )

    # --- 9. ขั้นตอนบันทึกวันเวลานัดหมาย ---
    elif current_step == "WAITING_DATETIME":
      appointment_time = clean_text(user_text)
      if not appointment_time or len(appointment_time) < 3:
        reply_msg = TextMessage(
            text="กรุณาระบุวันและเวลาที่สะดวกให้ชัดเจนอีกครั้งนะคะ เช่น วันเสาร์นี้ 14:00 น."
        )
        line_bot_api.reply_message(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg])
        )
        return
      name = session.get("name", "คนไข้ผ่าน LINE")
      phone = session.get("phone", "-")
      symptom = session.get("symptom", "ไม่ได้ระบุ")
      timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

      row = [
          timestamp,
          user_id,
          name,
          phone,
          symptom,
          appointment_time,
          "รอยืนยัน",
          "นัดหมายผ่าน LINE",
      ]
      if _sync_booking_to_sheets(row):
        patient_profile_cache[user_id] = {
            "data": {
                "name": name,
                "phone": phone,
                "last_symptom": clean_symptom_text(symptom),
            },
            "time": time.time(),
        }
        user_sessions.pop(user_id, None)
        reply_msg = TextMessage(
            text=(
                "✅ บันทึกคำขอนัดหมายเรียบร้อยแล้วค่ะ!\n\n"
              f"👤 ชื่อ: {name}\n"
              f"📞 เบอร์โทร: {phone}\n"
              f"🩺 อาการ: {symptom}\n"
              f"📅 วัน-เวลาที่สะดวก: {appointment_time}\n\n"
              "ทางคลินิกจะตรวจสอบตารางนัดและติดต่อยืนยันคิวให้โดยเร็วที่สุดค่ะ ขอบคุณค่ะ 🙏\n\n"
              "(หากต้องการแก้ไขชื่อหรือเบอร์โทร สามารถพิมพ์ 'แก้ไขข้อมูล' ได้ตลอดเวลานะคะ)"
            )
        )
      else:
        reply_msg = TextMessage(
            text=(
                "ขออภัยค่ะ ระบบยังบันทึกคำขอนัดหมายไม่สำเร็จ\n"
                "กรุณาลองส่งวันและเวลาที่สะดวกอีกครั้งในภายหลังนะคะ"
            )
        )

    # --- 10. กรณีถามคำถามอื่นๆ (ส่งให้ Gemini AI ตอบคำถามกายภาพบำบัด) ---
    else:
      reply_text = generate_gemini_response(user_text)

      if not reply_text:
        reply_text = (
            "ขออภัยค่ะ ระบบประมวลผลคำตอบขัดข้องชั่วคราว\n\n"
            "หากต้องการนัดหมายตรวจรักษากับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ"
        )

      reply_msg = TextMessage(text=reply_text)

    line_bot_api.reply_message(
        ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg])
    )


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8000))
  uvicorn.run("app:app", host="0.0.0.0", port=port)
