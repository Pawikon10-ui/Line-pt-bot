from datetime import datetime
import os
import re
from fastapi import FastAPI, Header, HTTPException, Request
from google import genai
# import gspread
from google.oauth2.service_account import Credentials
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
import os 	
CHANNEL_SECRET = "2636c41903dc0f636d6ebcf87f6a4dba"
CHANNEL_ACCESS_TOKEN = "iVq/zXeOkyImYHGBHyw0cUv+3RgZ+Xl2BCLzI64N6QER8VrDUsAR79yTubyn3MYNm1jml2Zd4h8HYJZEiU+tpw/PJUgJLeyR0B/OdKb3aQe/oSdbpzDQjiTfm8iCLjGstlNiAEtXbl3ccYbWxjgbIAdB04t89/1O/w1cDnyilFU="

# 👉 นำ Gemini API Key ที่ได้จาก Google AI Studio มาวางตรงนี้ครับ:
GEMINI_API_KEY = "193ac59ecb0f61195778652b5211ea0eb1fae5f5"

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

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

user_sessions = {}


def clean_text(text: str) -> str:
  """ตัดคำลงท้ายออกจากท้ายประโยค"""
  pattern = (
      r"(\s*(นะคะ|นะครับ|ครับผม|ค่ะ|ครับ|ค่า|ค้า|ค๊า|คับ|คั้บ|งับ|จ้า|จ้ะ|ฮะ|นะ))+\s*$"
  )
  return re.sub(pattern, "", text.strip()).strip()


def get_patient_profile(user_id: str):
  """ค้นหาประวัติคนไข้เดิมจาก Google Sheet ด้วย LINE User ID"""
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
      "📅 พิมพ์ 'จองคิว' เพื่อทำการนัดหมายตรวจประเมินกับนักกายภาพบำบัดค่ะ\n\n"
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

  # --- ขั้นที่ 1: ตรวจจับคำว่า "จองคิว" หรือ "นัดหมาย" ---
  if any(keyword in user_text for keyword in ["จองคิว", "นัดหมาย", "ขอนัด"]):
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
              f"ยินดีต้อนรับกลับค่ะ คุณ {profile['name']} 🏥\n\n"
              f"ต้องการนัดหมายรักษาอาการเดิม ({profile['last_symptom']})"
              " หรือมีอาการใหม่แจ้งเพิ่มเติมคะ?"
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
    if "อาการเดิม" in user_text:
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
            "(ตัวอย่าง: ทรัสมี คลินิก 082xxxxxxx)"
        )
    )

  # --- สำหรับคนไข้ใหม่: รอรับชื่อและเบอร์ ---
  elif current_step == "WAITING_CONTACT":
    phone_match = re.search(r"0[689]\d{1}[- ]?\d{3}[- ]?\d{4}|0\d{9}", user_text)
    if phone_match:
      raw_phone = phone_match.group()
      phone = raw_phone.replace("-", "").replace(" ", "")
      raw_name = user_text.replace(raw_phone, "").strip()
      name = re.sub(r"^(ชื่อ|คุณ|ติดต่อ)\s*[:\s]*", "", raw_name).strip()
      name = clean_text(name)
      if not name:
        name = "คนไข้ผ่าน LINE"

      session["name"] = name
      session["phone"] = phone
      session["step"] = "WAITING_DATETIME"
      user_sessions[user_id] = session

      reply_msg = TextMessage(
          text=(
              f"ยินดีค่ะ คุณ {name}\n\n"
              "3/3 ขั้นตอนสุดท้าย: สะดวกเข้ามาตรวจประเมินวันและเวลาใดดีคะ?\n"
              "(ตัวอย่าง: วันเสาร์นี้ 14:00 น., 22 ก.ย. ช่วงบ่าย)"
          )
      )
    else:
      reply_msg = TextMessage(
          text=(
              "ขออภัยค่ะ ระบบไม่พบเบอร์โทรศัพท์ 10 หลัก\n"
              "กรุณาพิมพ์ 'ชื่อ-นามสกุล และเบอร์โทรศัพท์' ใหม่อีกครั้งนะคะ"
          )
      )

  # --- ขั้นตอนบันทึกวันเวลานัดหมาย ---
  elif current_step == "WAITING_DATETIME":
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
    sheet.append_row(row)
    user_sessions.pop(user_id, None)

    reply_msg = TextMessage(
        text=(
            "✅ บันทึกคำขอนัดหมายเรียบร้อยแล้วค่ะ!\n\n"
            f"👤 ชื่อ: {name}\n"
            f"📞 เบอร์โทร: {phone}\n"
            f"🩺 อาการ: {symptom}\n"
            f"📅 วัน-เวลาที่สะดวก: {appointment_time}\n\n"
            "ทางคลินิกจะตรวจสอบตารางนัดและติดต่อยืนยันคิวให้โดยเร็วที่สุดค่ะ ขอบคุณค่ะ 🙏"
        )
    )

  # --- กรณีถามคำถามอื่นๆ (ส่งให้ Gemini AI ตอบคำถามกายภาพบำบัด พร้อม Fallback ป้องกัน 503) ---
  else:
    reply_text = None
    candidate_models = ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.6-flash"]
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
