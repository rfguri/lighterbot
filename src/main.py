import asyncio
import os
from dotenv import load_dotenv
import lighter
import eth_account

load_dotenv()

BASE_URL = os.getenv("BASE_URL")
ETH_PRIVATE_KEY = os.getenv("ETH_PRIVATE_KEY")
API_KEY_INDEX = int(os.getenv("API_KEY_INDEX"))
API_KEY_PRIVATE_KEY = os.getenv("API_KEY_PRIVATE_KEY")


async def main():
    if not ETH_PRIVATE_KEY or not API_KEY_PRIVATE_KEY:
        raise RuntimeError(
            "Missing keys in .env")

    api_client = lighter.ApiClient(
        configuration=lighter.Configuration(host=BASE_URL))

    l1_address = eth_account.Account.from_key(ETH_PRIVATE_KEY).address
    resp = await lighter.AccountApi(api_client).accounts_by_l1_address(l1_address=l1_address)
    if not resp.sub_accounts:
        raise RuntimeError(f"No sub_accounts found for {l1_address}")

    account_index = resp.sub_accounts[0].index

    signer = lighter.SignerClient(
        url=BASE_URL,
        private_key=API_KEY_PRIVATE_KEY,
        account_index=account_index,
        api_key_index=API_KEY_INDEX,
    )

    print(
        f"[lighterbot] ready | account_index={account_index} api_key_index={API_KEY_INDEX}")

    await signer.close()
    await api_client.close()

if __name__ == "__main__":
    asyncio.run(main())
