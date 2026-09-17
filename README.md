# ✈️ 機票價格追蹤器

每天自動查你指定的航線票價，存成歷史紀錄，發現便宜票就推到你手機上。
出發地和目的地都可以填**多個點**，也支援**多段行程**（雪梨進、黃金海岸出）。

通知走 **LINE** 和 **ntfy.sh**，而且可以**直接從 ntfy 發訊息改航線**，不用開 GitHub。

```
每天 09:00（台灣時間）
   ↓
GitHub Actions 跑 tracker
   ↓
查 Google Flights 票價（fast-flights）
   ↓
寫進 data/prices.csv（commit 回 repo，等於免費的歷史資料庫）
   ↓
低於門檻 or 比近期中位數便宜一截 → 推 LINE / ntfy
```

---

## 快速開始

```bash
pip install -e .

# 臨時查一次（不用設定檔）
python -m tracker.cli query TPE SYD --depart 2026-12-20 --nights 12

# 一個出發地比多個目的地，找最便宜的那個
python -m tracker.cli query TPE SYD,OOL,BNE --depart 2026-12-20 --nights 12

# 先看看會查哪些組合，不連網
python -m tracker.cli run --dry-run
```

目前 `routes.yaml` 追的是 **台北 → 雪梨(SYD) / 黃金海岸(OOL)**，
日期是預填的，請改成你真正要飛的區間。

---

## 設定航線：`routes.yaml`

這是唯一需要你編輯的檔案。目前設好的是台北→澳洲東岸，換航線就直接改它。
檔案末尾有其他寫法的備忘（多出發地、單程、排除組合）。

機場代碼用 IATA 三碼。也可以用**城市代碼**一次涵蓋多個機場：`TYO` = NRT + HND、`NYC` = JFK + LGA + EWR。
澳洲這幾個各自是獨立機場，沒有共用城市代碼：`SYD` 雪梨、`OOL` 黃金海岸、`BNE` 布里斯本、`MEL` 墨爾本。

### 模式 A：單純兩地

```yaml
routes:
  - name: 台北-雪梨
    from: TPE
    to: SYD
    trip: round               # round | oneway | multi
    windows:
      - depart: 2026-12-20
        nights: 12
    alert_below: 26000        # 低於這個價（TWD）就通知
    alert_drop_pct: 12        # 或比近 30 天中位數便宜 12% 也通知
```

兩個門檻是**獨立**的，設一個或兩個都可以，任一成立就通知。
`alert_drop_pct` 需要累積至少 3 筆同行程的歷史才會生效——第一次跑不會因為沒有基準線就亂叫。

> 不確定門檻該填多少就先隨便抓一個，跑一兩週後 `alert_drop_pct` 會自己抓到行情，
> 再回頭用 `report` 看實際價格帶去修 `alert_below`。

### 模式 B：多個點互相比價

`from` 和 `to` 都吃清單，會做笛卡兒展開。這就是目前設定檔在做的事：

```yaml
  - name: 台北-澳洲東岸
    from: TPE
    to: [SYD, OOL]            # 2 個目的地 = 2 條航線
    trip: round
    compare: true             # 報表會把這組依目的地排名
    windows:
      - depart_range: [2026-12-12, 2026-12-26]   # 區間內每天各查一次
        nights: 12
    alert_below: 26000
```

設了 `compare: true` 之後：

```bash
python -m tracker.cli report --group 台北-澳洲東岸
```

輸出長這樣（數字為格式示意，不是實際查到的票價）：

```
近 30 天最低價（群組：台北-澳洲東岸，每個目的地取最低）
────────────────────────────────────────────────────────────
 1. TPE>OOL>TPE    23,480 TWD  2026-12-15~2026-12-27  Scoot/Jetstar
 2. TPE>SYD>TPE    27,900 TWD  2026-12-18~2026-12-30  China Airlines
```

多出發地、排除組合的寫法：

```yaml
    from: [TPE, KHH]          # 2 個出發地 × 2 個目的地 = 4 條航線
    to: [SYD, OOL]
    exclude:
      - [KHH, OOL]            # 排除不想要的組合
```

> ⚠️ **會爆量**：`2 個出發地 × 3 個目的地 × 31 天 = 186 次查詢`。
> 超過 `max_queries`（預設 60）會直接中止並告訴你實際數量，不會硬跑到被 Google 擋。
> 先用 `--dry-run` 看展開結果。

### 模式 C：多段行程

進出不同城市，一張票：

```yaml
  - name: 雪梨進黃金海岸出
    trip: multi
    legs:
      - { from: TPE, to: SYD, depart: 2026-12-12 }
      - { from: OOL, to: TPE, depart: 2026-12-24 }
    alert_below: 30000
```

---

## 設定通知

兩個管道都是**設了才啟用**，可以只設一個、也可以兩個都設。
在 GitHub repo 的 **Settings → Secrets and variables → Actions → New repository secret** 填。

都沒設的話，在 Actions 裡會退回開 GitHub Issue（手機裝 GitHub App 也會收到推播），不會把便宜票默默丟掉。

### ntfy.sh（3 分鐘，推薦先設這個）

1. 產一個夠亂的主題名：

   ```bash
   python3 -c "import secrets; print('flight-' + secrets.token_hex(8))"
   ```

2. 手機裝 [ntfy App](https://ntfy.sh/)（[iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)），按 + 訂閱剛剛那個主題名。
3. 把主題名存成 secret `NTFY_TOPIC`。

> 🔒 **主題名就是密碼**。在公開的 ntfy.sh 上，任何知道主題名的人都能讀你的通知、也能發訊息給你。
> 所以一定要用隨機字串，不要取 `flights` 這種。想更安全就自架 ntfy 並設 `NTFY_SERVER`。

| Secret | 說明 |
|---|---|
| `NTFY_TOPIC` | 必填，你的隨機主題名 |
| `NTFY_SERVER` | 選填，自架伺服器網址，預設 `https://ntfy.sh` |

### LINE（約 30 分鐘，通知進你真的會看的 App）

LINE Notify 已於 2025-03-31 終止服務，所以要改走 Messaging API：

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 用 LINE 帳號登入。
2. 建一個 **Provider**（名字隨便取，例如 `personal`）。
3. 在該 Provider 底下建一個 **Messaging API channel**。這會同時開一個 LINE 官方帳號。
4. 進 channel 的 **Messaging API** 分頁：
   - 往下找 **Channel access token (long-lived)**，按 **Issue** 產生 token → 存成 secret `LINE_CHANNEL_TOKEN`
   - 用手機掃同一頁的 QR code，**把這個官方帳號加為好友**（沒加好友就推不了訊息）
5. 拿你自己的 `userId`：在 **Basic settings** 分頁最下方的 **Your user ID**（`U` 開頭的一串）→ 存成 secret `LINE_USER_ID`

| Secret | 說明 |
|---|---|
| `LINE_CHANNEL_TOKEN` | Channel access token (long-lived) |
| `LINE_USER_ID` | 你自己的 user ID，`U` 開頭 |

推播訊息會算進官方帳號的每月免費額度。一個月幾則機票通知遠遠用不完。

### 驗證通知有沒有設對

不用等真的有便宜票：

```bash
NTFY_TOPIC=你的主題名 python -m tracker.cli test-notify
```

```
偵測到管道：ntfy
  ✓ ntfy
```

在 GitHub 上則是到 **Actions → Track flight prices → Run workflow** 手動觸發一次。

---

## 從手機改航線（ntfy 指令頻道）

不用開 GitHub、不用架伺服器。在 ntfy App 打一則訊息，排程的 job 每 30 分鐘讀一次並改設定檔。

ntfy 是**雙向**的 pub/sub，所以「收通知」和「發指令」可以共用同一套東西——
差別只在多開一個 topic 當收件匣，由 GitHub Actions 去輪詢它。

### 設定

1. 再產一個**跟通知用的不一樣**的隨機 topic 當指令頻道：

   ```bash
   python3 -c "import secrets; print('cmd-' + secrets.token_hex(8))"
   ```

2. 產一組通關碼：

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(6))"
   ```

3. 存成 secrets，並在 ntfy App 訂閱這個新 topic（要用它來發訊息）：

| Secret | 說明 |
|---|---|
| `NTFY_COMMAND_TOPIC` | 指令頻道的 topic 名 |
| `NTFY_COMMAND_SECRET` | 通關碼，每則指令都要帶 |

> 🔒 **為什麼指令要再加一道通關碼？**
> topic 名稱等於密碼，而任何知道它的人都能往裡面**發**訊息。
> 讀通知被看到頂多是隱私問題；能發指令就能改你的設定、把查詢量灌爆。
> 所以沒設 `NTFY_COMMAND_SECRET` 時，程式**一則指令都不會處理**，而不是退而求其次只靠 topic 名。

### 用法

每則訊息的格式是 `<通關碼> /指令`：

```
xK9mQ2 /add BNE
xK9mQ2 /scan 2027-03-01 2027-03-20 14
xK9mQ2 /run
```

| 指令 | 作用 |
|---|---|
| `/routes` | 看目前設定 |
| `/scan 起日 迄日 晚數` | 改掃描區間，例：`/scan 2027-03-01 2027-03-20 14` |
| `/scan 出發日 晚數` | 只掃一天 |
| `/nights 晚數` | 只改待幾晚 |
| `/add 代碼` | 加目的地，例：`/add BNE` |
| `/rm 代碼` | 移除目的地 |
| `/price 金額` | 改通知門檻 |
| `/drop 百分比` | 改跌價通知門檻 |
| `/stops 次數` | 改轉機次數上限 |
| `/run` | 立刻查一輪，不等排程 |
| `/report` | 看最低價排名 |
| `/help` | 指令說明 |

有多條路線時用 `@名稱` 指定，例如 `xK9mQ2 /add BNE @台北-澳洲東岸`。
不指定的話套用到第一條非多段行程的路線。

執行結果會回到**通知用的那個 topic**（不是指令頻道）：指令進、回覆出，兩個方向分開。

### 安全網

- **改壞的設定不會生效**：每次編輯都先在暫存檔驗證過才寫入，語法錯誤或路線不合法會整個退回。
- **查詢量會擋**：`/scan 2027-01-01 2027-12-31 12` 會回你「改了會變成 731 次查詢，超過上限 60，沒有套用」，檔案原封不動。
- **註解不會被洗掉**：用 round-trip YAML 編輯，只有你真正改的那一行會變，行內註解和 `[SYD, OOL]` 這種寫法都保留。
- **不會重複執行**：處理過的訊息記在 `data/command_cursor.json`。
- **改動有紀錄**：每次套用都會 commit 一筆，`git log routes.yaml` 可以看到誰在什麼時候改了什麼。

### 限制

- **最多延遲 30 分鐘**才生效（排程間隔）。急的話 workflow 頁面可以手動觸發。
- **ntfy 免費版只快取 12 小時**。輪詢正常跑的話完全不影響；但如果 Actions 停掉超過 12 小時，那段時間發的指令會直接消失，不會補做。
- 解析失敗的指令會回一則 `✗ 原因` 給你，游標照樣往前走——同一則訊息不會每 30 分鐘重試並重複噴錯。

---

## 自動排程

`.github/workflows/track.yml` 已經設好每天 01:00 UTC（台灣早上 9 點）跑一次，
跑完把 `data/` 底下的價格紀錄 commit 回同一個分支。

改時間就改 cron（注意是 **UTC**）：

```yaml
on:
  schedule:
    - cron: '0 1 * * *'     # 每天 09:00 台灣時間
    # - cron: '0 1,13 * * *'  # 一天兩次，09:00 和 21:00
```

---

## 指令一覽

| 指令 | 用途 |
|---|---|
| `run` | 依 `routes.yaml` 跑完整一輪，寫歷史 + 發通知 |
| `run --dry-run` | 只列出會查哪些組合，不連網、不寫檔、不通知 |
| `query 出發地 目的地` | 臨時查一次，多個點用逗號分隔 |
| `report` | 從歷史紀錄印出最低價排名 |
| `report --group 名稱` | 某個比價群組，依目的地排名 |
| `test-notify` | 對所有已設定的管道送測試訊息 |
| `commands` | 讀 ntfy 指令頻道並套用（排程自動跑，平常不用手動下） |
| `url 出發地 目的地 --depart ...` | 只印 Google Flights 連結，不查價 |

常用參數：`--nights` `--oneway` `--adults` `--seat` `--max-stops` `--currency` `--depart-range 起:迄`

---

## 資料來源

| 來源 | 狀態 | 說明 |
|---|---|---|
| **fast-flights** | 預設 | 逆向 Google Flights 的 protobuf 查詢，免金鑰、真實票價。非官方，Google 改版時可能失效 |
| **SerpApi** | 選用 fallback | 設了 secret `SERPAPI_KEY` 就自動接上。官方支援、穩定，但按次計費，所以只有在 fast-flights 失敗後才會呼叫 |

一輪裡個別查詢失敗（某天賣光、某條航線不存在）只會記錄下來繼續跑；只有**整輪都沒查到任何票價**才算失敗。
查詢之間會隨機等 2–4 秒，避免被 Google 當成機器人。

### 關於 MCP

Claude 的連接器目錄裡有 Kiwi.com（`https://mcp.kiwi.com`，免金鑰）、Expedia、lastminute.com 等航班 MCP server，
**適合你在對話裡臨時問機票**。但它們回傳的是給 LLM 讀的自然語言，在無人值守的排程環境裡沒辦法穩定解析成數字，
所以這個追蹤器直接打資料來源，不透過 MCP。兩者可以並存。

---

## 資料檔

| 檔案 | 內容 |
|---|---|
| `data/prices.csv` | 價格歷史，只增不改。每次跑每組行程存最便宜的 3 筆 |
| `data/alerts.json` | 已通知過什麼，用來去重 |
| `data/command_cursor.json` | 指令頻道讀到哪則訊息了 |

去重規則：同一組行程 24 小時內不重複通知，**除非又跌了 5% 以上**（那是新消息）。

---

## 開發

```bash
pip install -e ".[dev]"
pytest
```

測試全部離線，用錄下來的資料結構跑，不會打網路。
