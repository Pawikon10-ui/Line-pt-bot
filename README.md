# LINE PT Bot

## Local setup

1. Use Python 3.11 or newer and create a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, then set every required value.
4. Keep the Google service-account file outside Git and set `GOOGLE_APPLICATION_CREDENTIALS` to its path.

The app deliberately returns a failed booking response if Google Sheets is unavailable or rejects a write. It must not claim a booking succeeded without durable storage.

## Configuration

Required for the LINE webhook: `LINE_CHANNEL_SECRET` and `LINE_CHANNEL_ACCESS_TOKEN`.

The previous LINE credentials were present in Git-tracked source code. Rotate both values in LINE Developers before deploying this revision, then store the replacements only as environment variables.

Required for bookings: `GOOGLE_SPREADSHEET_ID` and either `GOOGLE_APPLICATION_CREDENTIALS` or `GOOGLE_SERVICE_ACCOUNT_JSON`.

`GEMINI_API_KEY` is optional; without it, AI consultation replies fall back to a short error message.

## Test Sheets safely

`test_sheets.py` is an opt-in integration test. It never writes data unless `RUN_SHEETS_INTEGRATION_TEST=1` is explicitly set.
