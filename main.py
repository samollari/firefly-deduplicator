import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, BackgroundTasks
import apprise
from firefly_iii_client import ApiClient, Configuration
from firefly_iii_client.api import transactions_api

# Change your model imports to look like this:
from firefly_iii_client.models.transaction_store import TransactionStore
from firefly_iii_client.models.transaction_split_store import TransactionSplitStore

app = FastAPI()

# Configuration from Environment Variables (with defaults)
FIREFLY_URL = os.getenv("FIREFLY_III_URL")
FIREFLY_TOKEN = os.getenv("FIREFLY_III_ACCESS_TOKEN")
APPRISE_URL = os.getenv("APPRISE_URL")  # e.g., discord://webhook_id/webhook_token

# Fallback defaults if not provided in compose file
SEARCH_WINDOW_DAYS = int(os.getenv("SEARCH_WINDOW_DAYS", "7"))
BANK_KEYWORDS = [
    k.strip().upper()
    for k in os.getenv(
        "BANK_KEYWORDS", "CREDIT CARD SYSTEM,ONLINE PMT,AUTOPAY CC"
    ).split(",")
]

# Configure Firefly III Client
config = Configuration(host=f"{FIREFLY_URL}/api")
config.api_key["Authorization"] = f"Bearer {FIREFLY_TOKEN}"

# Initialize Apprise
apobj = apprise.Apprise()
if APPRISE_URL:
    apobj.add(APPRISE_URL)


def send_notification(title: str, message: str):
    if APPRISE_URL:
        try:
            apobj.notify(title=title, body=message)
        except Exception as e:
            print(f"Failed to send notification via Apprise: {e}")


def deduplicate_transaction(
    transaction_id: str, amount: float, description: str, date_str: str
):
    # 1. Filter using dynamically injected bank keywords
    if not any(keyword in description.upper() for keyword in BANK_KEYWORDS):
        return

    target_date = datetime.fromisoformat(date_str.split("T")[0])

    # 2. Use the injected search window
    start_date = (target_date - timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    end_date = (target_date + timedelta(days=SEARCH_WINDOW_DAYS)).strftime("%Y-%m-%d")
    inverse_amount = -amount

    with ApiClient(config) as api_client:
        api_instance = transactions_api.TransactionsApi(api_client)

        try:
            # 3. Safety Check for existing transfers
            existing_transfers = api_instance.get_transactions_by_search(
                query=f"type:transfer amount:{abs(amount)}",
                start=start_date,
                end=end_date,
            )
            if existing_transfers.get("data"):
                print("Matching transfer already exists. Skipping.")
                return

            # 4. Find the inverse transaction
            potential_matches = api_instance.get_transactions_by_search(
                query=f"amount:{inverse_amount}", start=start_date, end=end_date
            )

            for match in potential_matches.get("data", []):
                attributes = match.get("attributes", {})
                transactions = attributes.get("transactions", [])
                if not transactions:
                    continue

                match_data = transactions[0]
                match_id = match.get("id")
                match_desc = match_data.get("description", "")

                # Verify the found match also shares your bank keywords
                if any(keyword in match_desc.upper() for keyword in BANK_KEYWORDS):
                    print(f"Found match: {transaction_id} and {match_id}")

                    if amount < 0:
                        source_id = match_data.get("account_id")
                        destination_id = match_data.get(
                            "account_id"
                        )  # Swap/map as needed dynamically
                    else:
                        pass  # Asset/credit tracking mapping

                    # 5. Build Unified Transfer
                    transfer_split = TransactionSplitStore(
                        type="transfer",
                        date=date_str,
                        amount=str(abs(amount)),
                        description=f"Automated CC Payment: {description}",
                        source_id=source_id,
                        destination_id=destination_id,
                    )
                    transfer_store = TransactionStore(transactions=[transfer_split])
                    api_instance.store_transaction(transaction_store=transfer_store)

                    # 6. Housekeeping
                    api_instance.delete_transaction(id=transaction_id)
                    api_instance.delete_transaction(id=match_id)

                    send_notification(
                        title="Firefly III Deduplicator",
                        message=f"Success! Consolidated a credit card payment of ${abs(amount)}.",
                    )
                    break

        except Exception as e:
            print(f"Error processing deduplication: {e}")


@app.post("/webhook")
async def firefly_webhook(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    content = data.get("content", {})
    transactions = content.get("transactions", [])

    if not transactions:
        return {"status": "ignored"}

    tx = transactions[0]
    background_tasks.add_task(
        deduplicate_transaction,
        content.get("id"),
        float(tx.get("amount", 0)),
        tx.get("description", ""),
        tx.get("date", ""),
    )
    return {"status": "queued"}
