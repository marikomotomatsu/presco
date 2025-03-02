import requests
import time
import os
import io
import shutil
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 一意のuser-data-dirを作成
user_data_dir = "/tmp/chrome-user-data"
if os.path.exists(user_data_dir):
    shutil.rmtree(user_data_dir)  # フォルダを削除
os.makedirs(user_data_dir, exist_ok=True)  # 再作成

# Selenium の設定
options = webdriver.ChromeOptions()
options.binary_location = "/usr/bin/chromium-browser"  # Chromium のパスを明示的に指定
options.add_argument("--no-sandbox")  # サンドボックスを無効化
options.add_argument("--disable-dev-shm-usage")  # `/dev/shm` のメモリ制限回避
options.add_argument("--headless")  # ヘッドレスモード
options.add_argument("--disable-gpu")  # GPU 無効化
options.add_argument("--remote-debugging-port=9222")
options.add_argument(f"--user-data-dir={user_data_dir}")  # 安全な user-data-dir
options.add_argument("--disable-software-rasterizer")

# Chrome WebDriverをセットアップ
service = Service(ChromeDriverManager().install())
driver = webdriver.Chrome(service=service, options=options)

# GitHub SecretsからPRESCOログイン情報を復元
USERNAME = os.getenv("PRESCO_USERNAME")
PASSWORD = os.getenv("PRESCO_PASSWORD")
if USERNAME is None or PASSWORD is None:
    raise ValueError("ログイン情報が環境変数に設定されていません。")

# PRESCOログインページを開く
driver.get("https://presco.ai/partner/auth/loginForm")
time.sleep(3)

# PRESCOログイン情報を入力
driver.find_element(By.NAME, "username").send_keys(USERNAME)
driver.find_element(By.NAME, "password").send_keys(PASSWORD)

# ログインボタンをクリック
driver.find_element(By.XPATH, "//input[@type='submit']").click()
time.sleep(5)  # ログイン処理待機

# Seleniumのクッキーをrequestsに適用
session = requests.Session()
cookies = driver.get_cookies()
for cookie in cookies:
    session.cookies.set(cookie["name"], cookie["value"])

# SeleniumのUser-Agentをrequestsに適用
user_agent = driver.execute_script("return navigator.userAgent;")
session.headers.update({
    "User-Agent": user_agent,
    "Referer": "https://presco.ai/partner/actionLog/list",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
})

# 日付を指定してダウンロードURLを作成
today = datetime.today()
today_year = today.strftime("%Y")
today_month = today.strftime("%m")
today_date = today.strftime("%d")

# 昨日の日付
yesterday = today - timedelta(days=1)
yesterday_year = yesterday.strftime("%Y")
yesterday_month = yesterday.strftime("%m")
yesterday_date = yesterday.strftime("%d")

download_url = f"https://presco.ai/partner/actionLog/download?searchType=2&dateTimeFrom={yesterday_year}/{yesterday_month}/{yesterday_date}/01&dateTimeTo={today_year}/{today_month}/{today_date}"

# CSVデータをダウンロード
csv_response = session.get(download_url)

if csv_response.status_code == 200:
    print("CSVダウンロード成功！")
    
    # CSVデータをDataFrameに読み込む
    csv_data = pd.read_csv(io.StringIO(csv_response.text))

    # GitHub SecretsからGoogle認証情報を復元
    GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
    if GOOGLE_CREDENTIALS is None:
        raise ValueError("GOOGLE_CREDENTIALS が環境変数に設定されていません。")

    script_dir = os.path.dirname(os.path.abspath(__file__))  # 現在のパス
    credentials_path = os.path.join(script_dir, "config/presco-credentials.json")
    os.makedirs(os.path.dirname(credentials_path), exist_ok=True)
    with open(credentials_path, "w") as f:
        f.write(GOOGLE_CREDENTIALS)

    # Google Sheets APIに認証
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
    client = gspread.authorize(creds)

    # スプレッドシートを開く
    SPREADSHEET_ID = "1zCBRVmsHL01MsIrbpd_Egrni41jc4Aa176SBIN5Z5Sc"
    copy_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("presco_今月の成果")
    paste_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("presco_成果結果リスト")

    # スプレッドシートにアップロード
    copy_sheet.clear()  # 既存データをクリア
    csv_data = csv_data.fillna("")  # NaN を空白に変換
    copy_sheet.update([csv_data.columns.values.tolist()] + csv_data.values.tolist())

    # 既存データを取得
    paste_data = paste_sheet.get_all_values() 
    paste_df = pd.DataFrame(paste_data[1:], columns=paste_data[0])  # 最初の行をヘッダーとする
    paste_df.replace("", np.nan, inplace=True)
    paste_df = paste_df.dropna(subset=[paste_df.columns[0], paste_df.columns[1]]).reset_index(drop=True)
    paste_existing_pairs = set(zip(paste_df.iloc[:, 0], paste_df.iloc[:, 1]))  # (A列, B列) のタプルセットを作成

    # 新規データを取得
    copy_data = copy_sheet.get_all_values()  
    copy_df = pd.DataFrame(copy_data[1:], columns=copy_data[0])  # 最初の行をヘッダーとする
    filtered_copy_df = copy_df[~copy_df.apply(lambda row: (row.iloc[0], row.iloc[1]) in paste_existing_pairs, axis=1)]
    filtered_copy_df = filtered_copy_df.iloc[:, :19]  # A列からS列を取得
    new_values = filtered_copy_df.values.tolist()  # リスト化

    # Google Sheets の最大行数を取得
    current_row_count = paste_sheet.row_count

    # 貼り付け範囲を計算
    start_row = len(paste_df) + 2
    end_row = start_row + len(new_values) - 1
    range_to_update = f"A{start_row}:S{end_row}"

    # 新規データがスプレッドシートの最大行数を超えそうなら行を追加
    if end_row > current_row_count:
        extra_rows = end_row - current_row_count
        paste_sheet.add_rows(extra_rows)  # 必要な行を追加

    # AからS列の範囲を指定して貼り付け
    if new_values:
        paste_sheet.update(range_name=range_to_update, values=new_values, value_input_option="USER_ENTERED")
        print(f"スプレッドシートに新規データを追加しました。")
    else:
        print("新規データはありません。")
else:
    print("CSVのダウンロードに失敗しました。ステータスコード:", csv_response.status_code)

# ブラウザを閉じる
driver.quit()

