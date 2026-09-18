from datetime import datetime
import json
import os
import re
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
    TextMessage,
)
from linebot.v3.webhooks import FollowEvent, MessageEvent, TextMessageContent
import uvicorn

# --- 1. ข้อมูลการเชื่อมต่อ LINE & Gemini ---
CHANNEL_SECRET = os.getenv(
    "LINE_CHANNEL_SECRET", "2636c41903dc0f636d6ebcf87f6a4dba"
)
CHANNEL_ACCESS_TOKEN = os.getenv(
    "LINE_CHANNEL_ACCESS_TOKEN",
    "iVq/zXeOkyImYHGBHyw0cUv+3RgZ+Xl2BCLzI64N6QER8VrDUsAR79yTubyn3MYNm1jml2Zd4h8HYJZEiU+tpw/PJUgJLeyR0B/OdKb3aQe/oSdbpzDQjiTfm8iCLjGstlNiAEtXbl3ccYbWxjgbIAdB04t89/1O/w1cDnyilFU=",
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

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
5. ตอบให้กระชับ อ่านง่าย ใช้หัวข้อหรือ bullet point สั้นๆ
6. ในตอนท้ายของคำตอบ ให้เชิญชวนอย่างนุ่มนวลว่า "หากต้องการตรวจประเมินร่างกายอย่างละเอียดกับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ"
"""

# --- 2. ข้อมูลการเชื่อมต่อ Google Sheets ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SPREADSHEET_ID = "1NuGHeurpnpXnEefOV1iA8K627biu8itiupyLl2I7cpE"
sheet = None

# ตรวจสอบหาไฟล์ credentials.json ทั้งในเครื่อง Mac และบน Render (/etc/secrets/credentials.json)
creds_path = None
for path in [
    "credentials.json",
    "/etc/secrets/credentials.json",
    "/opt/render/project/src/credentials.json",
]:
  if os.path.exists(path):
    creds_path = path
    break

if creds_path:
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

EDIT_KEYWORDS = [
    "แก้ไขชื่อ",
    "เปลี่ยนชื่อ",
    "แก้ชื่อ",
    "แก้ไขเบอร์",
    "เปลี่ยนเบอร์",
    "แก้เบอร์",
    "แก้ไขข้อมูล",
    "แก้ข้อมูล",
    "เปลี่ยนข้อมูล",
    "อัปเดตข้อมูล",
    "อัปเดตเบอร์",
    "อัพเดทข้อมูล",
    "อัพเดทเบอร์",
    "อัพเดทชื่อ",
]


def clean_text(text: str) -> str:
  """ตัดคำลงท้ายออกจากท้ายประโยค"""
  pattern = (
      r"(\s*(นะคะ|นะครับ|ครับผม|ค่ะ|ครับ|ค่า|ค้า|ค๊า|คับ|คั้บ|งับ|จ้า|จ้ะ|ฮะ|นะ))+\s*$"
  )
  return re.sub(pattern, "", text.strip()).strip()


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
      r"^(ชื่อ|คุณ|ติดต่อ|แก้ไขเป็น|เปลี่ยนเป็น|เปลี่ยนชื่อเป็น|แก้ชื่อเป็น|แก้เบอร์เป็น|ชื่อใหม่|เบอร์ใหม่)\s*[:\s]*",
      "",
      raw_name,
  ).strip()
  cleaned_name = clean_text(cleaned_name)

  if cleaned_name and len(cleaned_name) >= 2:
    name = cleaned_name
  else:
    name = default_name

  if not phone:
    phone = default_phone

  return name, phone


def get_patient_profile(user_id: str):
  """ค้นหาประวัติคนไข้เดิมจาก Google Sheet ด้วย LINE User ID"""
  if not sheet:
    return None
  try:
    records = sheet.get_all_values()
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
            return {
                "name": r_name,
                "phone": r_phone,
                "last_symptom": r_symptom or "อาการเดิม",
            }
  except Exception as e:
    print(f"Error fetching profile: {e}")
  return None


def update_patient_profile(user_id: str, new_name: str, new_phone: str) -> bool:
  """อัปเดตชื่อและเบอร์โทรศัพท์ของคนไข้ใน Google Sheet"""
  if not sheet:
    return False
  try:
    records = sheet.get_all_values()
    target_row_idx = None
    # ค้นหาแถวล่าสุดของคนไข้ท่านนี้
    for idx, row in enumerate(records, start=1):
      if idx > 1 and len(row) >= 2 and row[1] == user_id:
        target_row_idx = idx

    if target_row_idx:
      # อัปเดตคอลัมน์ C (ชื่อ) และ D (เบอร์) ของแถวล่าสุด
      sheet.update(
          range_name=f"C{target_row_idx}:D{target_row_idx}",
          values=[[new_name, new_phone]],
      )
      return True
    else:
      # หากยังไม่มีแถว ให้สร้างแถวประวัติคนไข้ไว้
      timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
      row = [
          timestamp,
          user_id,
          new_name,
          new_phone,
          "-",
          "-",
          "บันทึกประวัติ",
          "อัปเดตข้อมูลผ่าน LINE",
      ]
      all_vals = sheet.get_all_values()
      next_r = len(all_vals) + 1
      for i, r_val in enumerate(all_vals, start=1):
        if i > 1 and not any(r_val):
          next_r = i
          break
      sheet.update(range_name=f"A{next_r}:H{next_r}", values=[row])
      return True
  except Exception as e:
    print(f"Error updating patient profile in Google Sheets: {e}")
    return False


app = FastAPI()


@app.post("/callback")
async def callback(
    request: Request, x_line_signature: str = Header(None, alias="x-line-signature")
):
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
      "⚙️ พิมพ์ 'แก้ไขข้อมูล' เพื่ออัปเดตชื่อหรือเบอร์โทรศัพท์ได้ตลอดเวลาค่ะ\n\n"
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

  session = user_sessions.get(user_id, {})
  current_step = session.get("step")
  reply_msg = None

  # --- ขั้นที่ 0: ตรวจจับคำสั่งแก้ไขข้อมูล / เปลี่ยนชื่อ / เปลี่ยนเบอร์ โดยตรง ---
  if any(k in user_text for k in EDIT_KEYWORDS) and current_step not in [
      "WAITING_SYMPTOM",
      "WAITING_NEW_SYMPTOM",
  ]:
    profile = get_patient_profile(user_id)
    curr_name = profile["name"] if profile else session.get("name")
    curr_phone = profile["phone"] if profile else session.get("phone")

    user_sessions[user_id] = {
        "step": "WAITING_EDIT_CONTACT",
        "name": curr_name,
        "phone": curr_phone,
        "last_symptom": (
            profile["last_symptom"] if profile else session.get("last_symptom")
        ),
        "symptom": session.get("symptom"),
    }

    if curr_name and curr_phone:
      reply_msg = TextMessage(
          text=(
              "ข้อมูลปัจจุบันของคุณในระบบ:\n"
              f"👤 ชื่อ: {curr_name}\n"
              f"📞 เบอร์โทร: {curr_phone}\n\n"
              "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่ที่ต้องการอัปเดตได้เลยค่ะ ✍️\n"
              "(สามารถพิมพ์ทั้งชื่อและเบอร์ เช่น 'สมชาย ใจดี 0891234567' หรือพิมพ์เฉพาะเบอร์ใหม่/ชื่อใหม่ก็ได้นะคะ)"
          )
      )
    else:
      reply_msg = TextMessage(
          text=(
              "ยังไม่พบข้อมูลประวัติเดิมในระบบค่ะ 🏥\n\n"
              "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' เพื่อลงทะเบียนข้อมูลไว้ได้เลยค่ะ ✍️\n"
              "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
          )
      )

  # --- กรณีอยู่ในขั้นตอนรอรับข้อมูลแก้ไขชื่อ/เบอร์ ---
  elif current_step == "WAITING_EDIT_CONTACT":
    curr_name = session.get("name")
    curr_phone = session.get("phone")
    new_name, new_phone = extract_contact_info(user_text, curr_name, curr_phone)

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

      if session.get("symptom"):
        session["step"] = "WAITING_DATETIME"
        user_sessions[user_id] = session
        reply_msg = TextMessage(
            text=(
                "✅ อัปเดตข้อมูลเรียบร้อยแล้วค่ะ! ✨\n\n"
                f"👤 ชื่อ: {new_name}\n"
                f"📞 เบอร์โทร: {new_phone}\n\n"
                "สะดวกเข้ามาตรวจประเมินวันและเวลาใดดีคะ?\n"
                "(ตัวอย่าง: วันเสาร์นี้ 14:00 น., 22 ก.ย. ช่วงบ่าย)"
            )
        )
      elif session.get("last_symptom"):
        session["step"] = "RETURNING_CHOICE"
        user_sessions[user_id] = session
        reply_msg = TextMessage(
            text=(
                "✅ อัปเดตข้อมูลเรียบร้อยแล้วค่ะ! ✨\n\n"
                f"👤 ชื่อ: {new_name}\n"
                f"📞 เบอร์โทร: {new_phone}\n\n"
                f"ต้องการนัดหมายรักษาอาการเดิม ({session.get('last_symptom')})\n"
                "หรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
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
                            label="แก้ไขชื่อ/เบอร์โทร", text="แก้ไขชื่อ/เบอร์โทร"
                        )
                    ),
                ]
            ),
        )
      else:
        user_sessions.pop(user_id, None)
        reply_msg = TextMessage(
            text=(
                "✅ อัปเดตข้อมูลเรียบร้อยแล้วค่ะ! ✨\n\n"
                f"👤 ชื่อ: {new_name}\n"
                f"📞 เบอร์โทร: {new_phone}\n\n"
                "หากต้องการนัดหมายตรวจรักษากับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ 🏥"
            )
        )

  # --- ขั้นที่ 1: ตรวจจับคำว่า \"จองคิว\" หรือ \"นัดหมาย\" ---
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
              "หรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
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
                          label="แก้ไขชื่อ/เบอร์โทร", text="แก้ไขชื่อ/เบอร์โทร"
                      )
                  ),
              ]
          ),
      )
    else:
      user_sessions[user_id] = {"step": "WAITING_SYMPTOM"}
      reply_msg = TextMessage(
          text=(
              "ยินดีต้อนรับสู่คลินิกกายภาพบำบัดค่ะ 🏥\n\n"
              "1/3 กรุณาพิมพ์ระบุอาการ หรือบริเวณที่มีปัญหาได้เลยค่ะ\n"
              "(เช่น ปวดสะบัก, ไหล่ติด, นิ้วล็อก, หมอนรองกระดูกทับเส้น เป็นต้น)"
          )
      )

  # --- กรณีคนไข้เดิมเลือกตัวเลือก ---
  elif current_step == "RETURNING_CHOICE":
    if any(k in user_text for k in ["แก้ไข", "เปลี่ยน", "เบอร์", "ชื่อ"]):
      session["step"] = "WAITING_EDIT_CONTACT"
      user_sessions[user_id] = session
      reply_msg = TextMessage(
          text=(
              "ข้อมูลปัจจุบันของคุณ:\n"
              f"👤 ชื่อ: {session.get('name')}\n"
              f"📞 เบอร์โทร: {session.get('phone')}\n\n"
              "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่ที่ต้องการแก้ไขได้เลยค่ะ ✍️\n"
              "(สามารถพิมพ์ทั้งชื่อและเบอร์ เช่น 'สมชาย ใจดี 0891234567' หรือพิมพ์เฉพาะเบอร์/ชื่อใหม่ก็ได้นะคะ)"
          )
      )
    elif "อาการเดิม" in user_text:
      session["step"] = "WAITING_DATETIME"
      session["symptom"] = f"{session.get('last_symptom')} (รักษาต่อเนื่อง)"
      user_sessions[user_id] = session

      reply_msg = TextMessage(
          text=(
              f"รับทราบค่ะคุณ {session.get('name')}\n\n"
              "สะดวกเข้ามาตรวจรักษาอาการเดิมวันและเวลาใดดีคะ?\n"
              "(ตัวอย่าง: พรุ่งนี้ 14:00 น., วันเสาร์ช่วงเช้า)"
          )
      )
    else:
      session["step"] = "WAITING_NEW_SYMPTOM"
      user_sessions[user_id] = session
      reply_msg = TextMessage(
          text="กรุณาพิมพ์ระบุอาการใหม่ที่ต้องการปรึกษาได้เลยค่ะ:"
      )

  # --- คนไข้เดิมพิมพ์อาการใหม่ ---
  elif current_step == "WAITING_NEW_SYMPTOM":
    clean_symptom = clean_text(user_text)
    session["symptom"] = clean_symptom
    session["step"] = "WAITING_DATETIME"
    user_sessions[user_id] = session

    reply_msg = TextMessage(
        text=(
            f"รับทราบอาการใหม่: '{clean_symptom}' ค่ะ\n\n"
            "สะดวกเข้ามาตรวจวันและเวลาใดดีคะ? (เช่น พรุ่งนี้บ่ายสอง, วันอาทิตย์)"
        )
    )

  # --- สำหรับคนไข้ใหม่: รอรับอาการ ---
  elif current_step == "WAITING_SYMPTOM":
    clean_symptom = clean_text(user_text)
    user_sessions[user_id] = {
        "step": "WAITING_CONTACT",
        "symptom": clean_symptom,
    }
    reply_msg = TextMessage(
        text=(
            f"รับทราบอาการ:\n'{clean_symptom}'\n\n"
            "2/3 กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' สำหรับติดต่อค่ะ\n"
            "(ตัวอย่าง: ปวีณ์กร การเร็ว 0826569179)"
        )
    )

  # --- สำหรับคนไข้ใหม่: รอรับชื่อและเบอร์ ---
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

  # --- ขั้นตอนบันทึกวันเวลานัดหมาย ---
  elif current_step == "WAITING_DATETIME":
    if any(k in user_text for k in EDIT_KEYWORDS):
      session["step"] = "WAITING_EDIT_CONTACT"
      user_sessions[user_id] = session
      reply_msg = TextMessage(
          text=(
              f"ข้อมูลปัจจุบัน: คุณ {session.get('name')} (เบอร์: {session.get('phone')})\n\n"
              "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่ที่ต้องการแก้ไขได้เลยค่ะ ✍️"
          )
      )
    else:
      appointment_time = clean_text(user_text)
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
      if sheet:
        try:
          all_vals = sheet.get_all_values()
          next_r = len(all_vals) + 1
          for i, r_val in enumerate(all_vals, start=1):
            if i > 1 and not any(r_val):
              next_r = i
              break
          sheet.update(range_name=f"A{next_r}:H{next_r}", values=[row])
        except Exception as e:
          print(f"Error updating row to Google Sheets: {e}")
          try:
            sheet.append_row(row)
          except Exception:
            pass
      else:
        print("Warning: Google Sheet is not connected. Skipping append_row.")

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

  # --- กรณีถามคำถามอื่นๆ (ส่งให้ Gemini AI ตอบคำถามกายภาพบำบัด) ---
  else:
    reply_text = None
    if ai_client:
      candidate_models = [
          "gemini-flash-latest",
          "gemini-3.5-flash",
          "gemini-3.6-flash",
      ]
      for model_name in candidate_models:
        try:
          ai_response = ai_client.models.generate_content(
              model=model_name,
              contents=user_text,
              config={"system_instruction": SYSTEM_INSTRUCTION},
          )
          if ai_response and ai_response.text:
            reply_text = ai_response.text
            break
        except Exception as err:
          print(f"Model {model_name} error: {err}")
          continue

    if not reply_text:
      reply_text = (
          "ขออภัยค่ะ ระบบประมวลผลคำตอบขัดข้องชั่วคราว\n\n"
          "หากต้องการนัดหมายตรวจรักษากับนักกายภาพบำบัด สามารถพิมพ์ 'จองคิว' ได้เลยนะคะ"
      )

    reply_msg = TextMessage(text=reply_text)

  with ApiClient(configuration) as api_client:
    line_bot_api = MessagingApi(api_client)
    line_bot_api.reply_message(
        ReplyMessageRequest(reply_token=event.reply_token, messages=[reply_msg])
    )


if __name__ == "__main__":
  uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
