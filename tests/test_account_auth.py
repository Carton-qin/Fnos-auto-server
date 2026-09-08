import asyncio
import os
import sys
import json
from httpx import AsyncClient, ASGITransport

# Ensure fnos-server root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.security import encrypt_credential, decrypt_credential, mask_credential
from app.captcha import solve_captcha_image
from app.database import init_db, get_db, AsyncSessionLocal
from app.models import Task
from app.crawler import SESSIONS_DIR, get_session_path, has_valid_session, get_session_updated_at
from main import app

async def test_all_account_auth_features():
    print("\n--- 1. Testing Security & Fernet Encryption ---")
    raw_pwd = "MySecretPassWord123!@#"
    encrypted = encrypt_credential(raw_pwd)
    assert encrypted != raw_pwd, "Encrypted string should differ from raw"
    decrypted = decrypt_credential(encrypted)
    assert decrypted == raw_pwd, "Decrypted string must match raw password"
    masked = mask_credential(raw_pwd)
    assert masked == "******", "Masked credential should be '******'"
    print("✅ Fernet encryption/decryption/masking passed!")

    print("\n--- 2. Testing ddddocr Engine ---")
    import ddddocr
    ocr = ddddocr.DdddOcr(show_ad=False)
    assert ocr is not None
    print("✅ ddddocr engine initialized successfully!")

    print("\n--- 3. Testing Database & Model Storage ---")
    await init_db()
    async with AsyncSessionLocal() as db:
        test_task_id = "test-auth-task-001"
        # Cleanup old test task if exists
        old = await db.get(Task, test_task_id)
        if old:
            await db.delete(old)
            await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer admin"}) as client:
        # Create task via API
        payload = {
            "id": "test-auth-task-001",
            "name": "测试账密自动托管任务",
            "type": "checkin",
            "schedule_type": "cron",
            "cron_expr": "0 8 * * *",
            "auth_mode": "playwright_account",
            "account_config": {
                "login_url": "https://httpbin.org/post",
                "username": "tester@example.com",
                "password": raw_pwd,
                "auto_solve_captcha": True
            },
            "params": {
                "url": "https://httpbin.org/get",
                "method": "GET"
            }
        }
        resp = await client.post("/api/tasks", json=payload)
        assert resp.status_code == 200, f"Failed to create task: {resp.text}"
        data = resp.json()
        assert data["ok"] is True
        print("✅ Task creation via API passed!")

        # Verify task list masks password and shows session status
        list_resp = await client.get("/api/tasks")
        assert list_resp.status_code == 200
        tasks = list_resp.json().get("tasks", [])
        target = next((t for t in tasks if t["id"] == "test-auth-task-001"), None)
        assert target is not None, "Created task must exist in task list"
        assert target["auth_mode"] == "playwright_account"
        assert target["account_config"]["password"] == "******", "Password must be masked in API output!"
        assert target["account_config"]["username"] == "tester@example.com"
        assert target["session_exists"] is False, "Initial session should not exist"
        print("✅ Task listing with password masking and session status passed!")

        # Test session simulation
        session_file = get_session_path("test-auth-task-001")
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "mock_sid", "value": "12345"}], "origins": []}, f)

        assert has_valid_session("test-auth-task-001") is True
        updated_at = get_session_updated_at("test-auth-task-001")
        assert updated_at is not None
        print(f"✅ Session persistence verified! Updated at: {updated_at}")

        # Verify task list reflects session_exists == True
        list_resp2 = await client.get("/api/tasks")
        target2 = next((t for t in list_resp2.json().get("tasks", []) if t["id"] == "test-auth-task-001"), None)
        assert target2["session_exists"] is True
        assert target2["session_updated_at"] is not None
        print("✅ API reflects updated session state!")

        # Cleanup
        if os.path.exists(session_file):
            os.remove(session_file)
        del_resp = await client.delete("/api/tasks/test-auth-task-001")
        assert del_resp.status_code == 200
        print("✅ Cleanup finished!")

if __name__ == "__main__":
    asyncio.run(test_all_account_auth_features())
