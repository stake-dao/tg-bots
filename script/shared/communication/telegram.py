import time


import requests


from shared.constants import Common, GlobalConstants


from shared.utils.globals import is_production


def send_telegram_message(botKey, chatId, message, parse_mode="html") -> bool:
    """Send a message to the Telegram channel with specified formatting.

    Returns True when Telegram accepted the message, False otherwise, so
    callers that need delivery guarantees (e.g. the vote reminder retry
    logic) can react; existing callers ignore the return value.
    """
    # Redirect production Telegram credentials to test channel when not in production mode
    if not is_production():
        prod_keys = {
            Common.TELEGRAM_API_KEY,
            Common.TELEGRAM_FEES_API_KEY,
            Common.TELEGRAM_ONLY_BOOST_API_KEY,
            GlobalConstants.BOT_API_KEY,
            GlobalConstants.BOT_VOTEMARKET_API_KEY,
            GlobalConstants.BOT_HARVESTS_API_KEY,
            GlobalConstants.VOTE_REMINDER_BOT_API_KEY,
        }
        if botKey in prod_keys:
            print(f"[DEV] Redirecting Telegram message to test channel (PROD=False)")
            botKey = Common.TEST_TELEGRAM_API_KEY
            chatId = Common.TEST_TELEGRAM_CHAT_ID

    url = f"https://api.telegram.org/bot{botKey}/sendMessage"
    payload = {
        "chat_id": chatId,
        "text": message,
        "parse_mode": parse_mode,
        "link_preview_options": {"is_disabled": True},
    }
    headers = {"Content-Type": "application/json"}

    success = False
    try:
        # Bounded: callers hold delivery leases (vote reminder in-flight
        # lock), so a hung request must never outlive them
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        success = True
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == 400:
            # Try to get more details about the error
            try:
                error_data = err.response.json()
                error_desc = error_data.get("description", "Unknown error")
                print(f"Telegram API Error 400: {error_desc}")

                # Handle supergroup migration
                if "group chat was upgraded to a supergroup" in error_desc:
                    print(
                        "⚠️  The Telegram group has been upgraded to a supergroup."
                    )
                    print(
                        "⚠️  You need to update the TELEGRAM_CHAT_ID in your configuration."
                    )
                    print("⚠️  To find the new chat ID:")
                    print(
                        "    1. Add your bot to the supergroup (if not already added)"
                    )
                    print("    2. Send a message in the group")
                    print(
                        "    3. Visit: https://api.telegram.org/bot{YOUR_BOT_TOKEN}/getUpdates"
                    )
                    print(
                        "    4. Look for the 'chat' object with a negative ID starting with -100"
                    )
                    return False

                # If it's an HTML parsing error, try sending without parse_mode
                if (
                    "can't parse" in error_desc.lower()
                    or "html" in error_desc.lower()
                ):
                    print("Retrying without HTML parse mode...")
                    payload["parse_mode"] = None
                    retry_response = requests.post(
                        url, json=payload, headers=headers, timeout=30
                    )
                    retry_response.raise_for_status()
                    return True
            except (ValueError, KeyError, requests.exceptions.JSONDecodeError):
                pass
        print(f"Failed to send the message: {err}")
    except requests.exceptions.RequestException as err:
        print(f"Failed to send the message: {err}")
    except Exception as e:
        print(f"An error occurred while sending the message: {e}")
    time.sleep(1)
    return success
