# ✈️ 機票價格追蹤器

每天自動查你指定的航線票價，存成歷史紀錄，發現便宜票就推到你手機上。
出發地和目的地都可以填**多個點**，也支援**多段行程**（雪梨進、布里斯本出），
可以限定只要直飛。

通知走 **Telegram**（推薦）、**LINE**、**ntfy.sh**，而且可以**直接傳訊息給 Telegram Bot（或 ntfy）改航線**，不用開 GitHub。

```
每天 09:00（台灣時間）
   ↓
GitHub Actions 跑 tracker
   ↓
查 Google Flights 票價（fast-flights）
   ↓
寫進 data/prices.csv（commit 回 repo，等於免費的歷史資料庫）
   ↓
低於門檻 or 比近期中位數便宜一截 → 推 Telegram / LINE / ntfy
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

目前 `routes.yaml` 追的是 **台北 → 雪梨(SYD) / 布里斯本(BNE)，只要直飛**，
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

`alert_drop_pct` 的基準線是**同一組行程在過去每次執行的最低價的中位數**，
需要累積至少 **3 次執行**（≈3 天）才會生效——第一次跑不會因為沒有基準線就亂叫。

> 注意這裡算的是「每次執行的最低價」，不是所有保留報價的中位數。
> 後者會把同一天不同選項的價差誤判成跌價，導致幾乎每次都通知。

> 不確定門檻該填多少就先隨便抓一個，跑一兩週後 `alert_drop_pct` 會自己抓到行情，
> 再回頭用 `report` 看實際價格帶去修 `alert_below`。

### 模式 B：多個點互相比價

`from` 和 `to` 都吃清單，會做笛卡兒展開：

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
    to: [SYD, BNE]
    exclude:
      - [KHH, BNE]            # 排除不想要的組合
```

**目的地需要不同設定時**（例如某條要放寬轉機、其他不用），拆成兩條路線、
共用同一個 `group`，報表仍會一起排名：

```yaml
  - name: 台北-雪梨
    group: 台北-澳洲東岸      # 兩條共用一個群組
    from: TPE
    to: SYD
    max_stops: 0             # 只要直飛

  - name: 台北-黃金海岸
    group: 台北-澳洲東岸
    from: TPE
    to: OOL
    max_stops: 2             # 沒直飛，要放寬才查得到
```

`compare: true` 等同於「自己一組」；明寫 `group:` 才能讓多條路線共用一組。

> ⚠️ **會爆量**：`2 個出發地 × 3 個目的地 × 31 天 = 186 次查詢`。
> 超過 `max_queries`（預設 60）會直接中止並告訴你實際數量，不會硬跑到被 Google 擋。
> 先用 `--dry-run` 看展開結果。

### 模式 C：多段行程

進出不同城市，一張票：

```yaml
  - name: 雪梨進布里斯本出
    trip: multi
    legs:
      - { from: TPE, to: SYD, depart: 2027-06-05 }
      - { from: BNE, to: TPE, depart: 2027-06-16 }
    alert_below: 50000
```

---

## 設定通知

三個管道都是**設了才啟用**，可以只設一個、也可以全設，通知會同時發到每一個已設定的管道。
在 GitHub repo 的 **Settings → Secrets and variables → Actions → New repository secret** 填。

都沒設的話，在 Actions 裡會退回開 GitHub Issue（手機裝 GitHub App 也會收到推播），不會把便宜票默默丟掉。

### Telegram（3 分鐘，推薦）

比 ntfy 少一個「裝額外 App、訂閱主題」的步驟——大部分人手機本來就有 Telegram。

1. 在 Telegram 裡搜尋 **@BotFather**，傳 `/newbot`，照指示取個名字。完成後會給你一段
   **bot token**（長得像 `123456789:ABCdefGhIJKlmNoPQRstuVwxyZ`）→ 存成 secret `TELEGRAM_BOT_TOKEN`。
2. 在 Telegram 搜尋你剛剛取的 bot 名稱，點進去按 **Start**，隨便傳一則訊息給它（例如 `hi`）——
   這一步是必要的，bot 在你主動開口之前沒辦法傳訊息給你。
3. 用瀏覽器打開（把 `<TOKEN>` 換成你的 bot token）：

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

   回傳的 JSON 裡找 `"chat":{"id":12345678,...}`，那個數字就是你的 chat id → 存成 secret `TELEGRAM_CHAT_ID`。

| Secret | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 必填，@BotFather 給的 bot token |
| `TELEGRAM_CHAT_ID` | 必填，你跟這個 bot 對話的 chat id（數字） |

> 🔒 bot token 等同密碼：拿到它的人可以用你的 bot 發訊息（但看不到你們的對話紀錄）。
> 洩漏了就回 @BotFather 用 `/revoke` 重發一組。

### ntfy.sh（3 分鐘，另一個選擇）

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

不用等真的有便宜票，也不用開終端機：到 **Actions → Test notifications → Run workflow**。
跑完在 Summary 會看到每個管道的成敗，以及指令頻道的 secret 有沒有設好（只報「已設定 / 未設定」，不會印出值）。

本機的話：

```bash
TELEGRAM_BOT_TOKEN=你的token TELEGRAM_CHAT_ID=你的chatid python -m tracker.cli test-notify
```

```
偵測到管道：telegram
  ✓ telegram
```

---

## 從手機改航線（指令頻道）

不用開 GitHub、不用架伺服器。傳一則訊息給 Telegram Bot（或 ntfy topic），
排程的 job 每 30 分鐘讀一次並改設定檔。**兩個管道用的是同一套指令引擎，設定好其中一個就能用**；
如果兩個都設定了，Telegram 優先。

Telegram 的 Bot API 跟 ntfy 一樣是雙向的：能推訊息給你，也能用 `getUpdates`
讀你傳給 bot 的訊息，所以「收通知」和「發指令」一樣可以共用同一個 bot、
不必另外架伺服器。

### 設定（Telegram，推薦）

如果你在〈設定通知〉已經設好 `TELEGRAM_BOT_TOKEN`，指令頻道就直接可以用了——
不用另外申請 bot，同一個 bot 兩用。只需要再加一組通關碼：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(6))"
```

| Secret | 說明 |
|---|---|
| `COMMAND_SECRET` | 通關碼，每則指令都要帶 |

> 🔒 **為什麼指令要再加一道通關碼？**
> bot token 外流的風險已經不小，但知道它的人也只能拿去**發通知**；
> 能下指令就能改你的設定、把查詢量灌爆，所以要多一層。
> 沒設 `COMMAND_SECRET` 時，程式**一則指令都不會處理**，不會退而求其次只靠 token 保密。

### 設定（ntfy，另一個選擇）

1. 再產一個**跟通知用的不一樣**的隨機 topic 當指令頻道：

   ```bash
   python3 -c "import secrets; print('cmd-' + secrets.token_hex(8))"
   ```

2. 產一組通關碼（跟上面 Telegram 那組共用同一個環境變數名稱 `COMMAND_SECRET` 也可以）：

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(6))"
   ```

3. 存成 secrets，並在 ntfy App 訂閱這個新 topic（要用它來發訊息）：

| Secret | 說明 |
|---|---|
| `NTFY_COMMAND_TOPIC` | 指令頻道的 topic 名 |
| `COMMAND_SECRET`（或舊名 `NTFY_COMMAND_SECRET`） | 通關碼，每則指令都要帶 |

> 🔒 topic 名稱等於密碼，而任何知道它的人都能往裡面**發**訊息，所以同樣需要通關碼保護——理由同上。

### 用法

每則訊息的格式是 `<通關碼> /指令`，傳給 Telegram Bot 或發到 ntfy topic 都一樣：

```
xK9mQ2 /add BNE
xK9mQ2 /scan 2027-03-01 2027-03-20 14
xK9mQ2 /run
```

| 指令 | 作用 |
|---|---|
| `/routes` | 看目前設定 |
| `/status` | 看設定＋每條路線最新查到的價格，一次看完整體現況 |
| `/scan 起日 迄日 晚數` | 改掃描區間，例：`/scan 2027-03-01 2027-03-20 14` |
| `/scan 出發日 晚數` | 只掃一天 |
| `/time ...` | `/scan` 的別名，改日期用這個名字比較好記 |
| `/nights 晚數` | 只改待幾晚 |
| `/add 代碼` | 加目的地，例：`/add BNE` |
| `/rm 代碼` | 移除目的地（最後一個不能移除，改用 `/to`） |
| `/to 代碼...` | **整個換掉**目的地，例：`/to BNE` 或 `/to SYD BNE OOL` |
| `/location 代碼...` | `/to` 的別名，換目的地用這個名字比較好記 |
| `/from 代碼...` | **整個換掉**出發地，例：`/from TPE KHH` |
| `/rename 新名稱` | 幫這條路線改名，例：`/rename 台北-布里斯本` |
| `/newroute 名稱 出發地 目的地 出發日 晚數 [門檻]` | **新增一整條新路線**，例：`/newroute 台北-福岡 TPE FUK 2027-06-05 7 20000` |
| `/delroute 名稱` | **刪除一整條路線**（不能刪到一條都不剩） |
| `/price 金額` | 改通知門檻 |
| `/drop 百分比` | 改跌價通知門檻 |
| `/stops 次數` | 改轉機次數上限，`0` 代表只要直飛 |
| `/run` | 立刻查一輪，不等排程 |
| `/report` | 近 30 天最低價，每條路線分開列、各取最便宜的 3 筆，附去回日期、航空公司、訂票連結 |
| `/reset` | 清空所有已通知紀錄，讓之前通知過的低價下次符合門檻能再通知一次 |
| `/help` | 指令說明 |

有多條路線時用 `@名稱` 指定，例如 `xK9mQ2 /add BNE @台北-澳洲東岸`。
不指定的話套用到第一條非多段行程的路線。`/to` `/from` `/rename` 對多段行程
路線（`trip: multi`）不生效，那種要直接改 `legs`。

`/add` `/rm` 是改**一條路線裡的目的地**；`/newroute` `/delroute` 是新增/刪除
**整條路線**，兩組指令的作用範圍不一樣，別搞混了。`/newroute` 只能建立來回
行程（沿用 `defaults` 的轉機上限等設定），單程或多段行程還是要直接編輯
`routes.yaml`。

`/reset` 清的是**通知去重紀錄**（`data/alerts.json`），不會動到 `routes.yaml`
或價格歷史；清掉後，同一個之前已經通知過的低價下次查到還是會再通知一次
（平常 24 小時內不重複通知、除非又跌 5% 以上的規則，`/reset` 之後短暫失效）。

**`/rm` 移不掉最後一個目的地** 是故意的：移光就沒東西可追了。真的要換成
別的地方，用 `/to` 一次整個換掉——這正是 OOL 完全沒有直飛航班、改追 BNE
時用的指令：

```
xK9mQ2 /stops 0 @台北-黃金海岸
xK9mQ2 /to BNE @台北-黃金海岸
xK9mQ2 /rename 台北-布里斯本 @台北-黃金海岸
```

三則指令分別是「轉機上限清成直飛」「目的地換成 BNE」「路線改名」，
跑完之後往後的指令就可以用新名稱 `@台北-布里斯本` 指定了。

執行結果會發到**每一個已設定的通知管道**（不是指令頻道）：指令進、回覆出，兩個方向分開，
同時開 Telegram 跟 LINE 的話兩邊都會收到回覆。

### 安全網

- **改壞的設定不會生效**：每次編輯都先在暫存檔驗證過才寫入，語法錯誤或路線不合法會整個退回。
- **查詢量會擋**：`/scan 2027-01-01 2027-12-31 12` 會回你「改了會變成 731 次查詢，超過上限 60，沒有套用」，檔案原封不動。
- **註解不會被洗掉**：用 round-trip YAML 編輯，只有你真正改的那一行會變，行內註解和 `[SYD, OOL]` 這種寫法都保留。
- **不會重複執行**：處理過的訊息記在 `data/command_cursor.json`。
- **改動有紀錄**：每次套用都會 commit 一筆，`git log routes.yaml` 可以看到誰在什麼時候改了什麼。

### 限制

- **最多延遲 30 分鐘**才生效（排程間隔）。急的話 workflow 頁面可以手動觸發，或看下面的
  Cloudflare Workers 選項把延遲壓到幾十秒內。
- **Telegram 的 `getUpdates` 大約保留 24 小時，ntfy 免費版只快取 12 小時**。輪詢正常跑的話完全不影響；
  但如果 Actions 停掉超過這個窗口，那段時間發的指令會直接消失，不會補做。
- 解析失敗的指令會回一則 `✗ 原因` 給你，游標照樣往前走——同一則訊息不會每 30 分鐘重試並重複噴錯。

### 更快的回應：Telegram Webhook + Cloudflare Workers（進階，選用）

預設的輪詢最多要等 30 分鐘才會處理你的指令。想要幾秒內就有回應，可以加一個
**Cloudflare Worker** 當「即時轉發器」：Telegram 一有新訊息就主動推給 Worker
（webhook，毫秒等級），Worker 立刻叫 GitHub 馬上跑 `commands.yml`，而不是等排程。

```
Telegram 傳訊息
   ↓（webhook，毫秒等級）
Cloudflare Worker（cloudflare/telegram-webhook/）
   ↓ 呼叫 GitHub API 觸發 workflow_dispatch，把訊息內容當參數帶過去
GitHub Actions 的 commands.yml 立刻開始跑（不用等 13、43 分）
   ↓
跟平常一樣：解析、驗證、寫入 routes.yaml、commit、回覆
```

Worker 本身**不重寫**任何 `routes.yaml` 解析或驗證邏輯——那些還是在
`tracker/commands.py` 裡，Worker 只負責兩件事：確認訊息帶對通關碼、
叫 GitHub 立刻跑一次。所以兩條路徑（排程輪詢／webhook 觸發）跑的是完全同一套
Python 邏輯，不會有邏輯分岔的風險。

> ⚠️ **這是模式切換，不是疊加**。Telegram 的規則是：一旦幫某個 bot 設定了
> webhook，那個 bot 的 `getUpdates`（也就是現有排程輪詢用的方法）**就會被
> Telegram 拒絕**，回傳 409 錯誤。所以設定好 webhook 之後，`commands.yml`
> 裡的 `schedule:` cron 觸發會開始每次都失敗（不影響安全性，但 Actions 頁面
> 會一直紅字）——請把那個 cron 拿掉或註解掉，只靠 webhook 觸發
> `workflow_dispatch`。這也代表原本「排程輪詢」提供的容錯（Worker 掛掉還有
> 下一次排程補救）沒有了，換成完全依賴 Cloudflare + 這組 GitHub token 的
> 可用性。

**設定步驟：**

1. 安裝 [wrangler](https://developers.cloudflare.com/workers/wrangler/)（Cloudflare 的 CLI）並登入：

   ```bash
   cd cloudflare/telegram-webhook
   npm install
   npx wrangler login
   ```

2. 產一組 GitHub **fine-grained personal access token**：GitHub → Settings →
   Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token。只勾這個 repo、只給 **Actions: Read and write** 權限，
   其他都不要給。

3. 設定三個 Cloudflare secret（都是互動輸入，不會出現在終端機歷史裡）：

   ```bash
   npx wrangler secret put GITHUB_TOKEN            # 貼上一步產生的 token
   npx wrangler secret put TELEGRAM_WEBHOOK_SECRET # 自己想一組隨機字串
   npx wrangler secret put COMMAND_SECRET          # 跟 GitHub secret 裡的 COMMAND_SECRET 一樣
   ```

4. 部署：

   ```bash
   npx wrangler deploy
   ```

   成功會印出一個網址，例如 `https://flight-tracker-telegram-webhook.<你的帳號>.workers.dev`。

5. 把這個網址註冊成 Telegram 的 webhook（`<TOKEN>` 換成你的 bot token，
   `<SECRET>` 換成上面設定的 `TELEGRAM_WEBHOOK_SECRET`，`<WORKER_URL>` 換成
   上一步印出的網址）：

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -d "url=<WORKER_URL>" \
     -d "secret_token=<SECRET>"
   ```

   回傳 `{"ok":true,...}` 就是成功了。可以用
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo` 隨時檢查目前設定。

6. **把 `commands.yml` 的 `schedule:` cron 拿掉或註解掉**（理由見上面的警告），
   只留 `workflow_dispatch:`。

**想切回輪詢模式：**

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook"
```

刪掉 webhook 之後，把 `commands.yml` 的 `schedule:` cron 加回來即可。

**這個 Worker 沒有自動化測試**——跟 Telegram 本身一樣，這個 sandbox 連不到
Cloudflare 或 Telegram 的網路，沒辦法在本機驗證，只能部署後照上面步驟實際測。
`GITHUB_TOKEN` 外流的風險要自己注意：它只能觸發這個 repo 的 workflow，範圍
已經盡量收窄，但仍然是一組活的憑證，不要 commit 進版控或貼到看得到的地方。

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
| `commands` | 讀指令頻道（Telegram 或 ntfy）並套用（排程自動跑，平常不用手動下） |
| `url 出發地 目的地 --depart ...` | 只印 Google Flights 連結，不查價 |

常用參數：`--nights` `--oneway` `--adults` `--seat` `--max-stops` `--currency` `--depart-range 起:迄`

---

## 資料來源

| 來源 | 狀態 | 說明 |
|---|---|---|
| **fast-flights** | 預設 | 逆向 Google Flights 的 protobuf 查詢，免金鑰、真實票價。非官方，Google 改版時可能失效 |
| **Kiwi.com** | 備援（已啟用） | 透過官方免金鑰的 MCP 端點 `https://mcp.kiwi.com`。涵蓋廉航，fast-flights 查不到的航線它常常查得到 |
| **SerpApi** | 選用 | 設了 secret `SERPAPI_KEY` 就自動接上。官方支援、穩定，但按次計費 |

`providers` 依序嘗試，**第一個查到票價的就採用**，所以後面的只有在前面失敗時才會被呼叫：

```yaml
providers: [fast_flights, kiwi]
```

雪梨這種 fast-flights 正常的航線根本不會碰到 Kiwi；只有黃金海岸那種整批失敗的才會落到它手上。

Kiwi 會賣兩種比較冒險的組合，兩者預設不同：

- **自行轉機**（self-transfer）：分開的兩張票，前段延誤要自己承擔——**預設排除**，這個風險不會因為票價便宜就變小。
- **轉機換機場**（同城市不同機場，例如降落墨爾本 MEL 後要跑到一小時車程外的 Avalon AVV 接下一班）：**預設允許**。差別只是多開一小時車，不是分開的兩張票；像黃金海岸這種單一張票選項很少的路線，排除它常常會變成永遠找不到能觸發門檻的票價。

兩者都可以在建構子覆寫：

```python
KiwiProvider(allow_self_transfer=True, allow_diff_airport_connection=False)
```

另外最便宜的票通常**只含手提行李**，跟含托運的票價不能直接比。

一輪裡個別查詢失敗（某天賣光、某條航線不存在）只會記錄下來繼續跑；只有**整輪都沒查到任何票價**才算失敗。
查詢之間會隨機等 2–4 秒，避免被 Google 當成機器人。

**整輪失敗會推一則通知到你手機**，而不是只讓 Actions 變紅。
這很重要：一個壞掉的追蹤器和一個「有在跑但都沒便宜票」的追蹤器，從手機上看起來一模一樣，
沒有這則通知你可能幾個月後才發現它早就死了。

---

## 已知問題與取捨

**黃金海岸(OOL) 現在沒被追蹤，因為它完全沒有直飛航班。**

2026-09-17 第一次實跑，fast-flights 對 OOL 整批查詢失敗（`TypeError: 'NoneType'
object is not subscriptable`），改用 Kiwi 查證後發現航班確實存在，只是 fast-flights
的解析在這條航線上壞掉——這部分已經修好，Kiwi 現在是 fast-flights 失敗時的備援
（見上面〈資料來源〉）。

但後來把設定改成**只要直飛**之後，用 Kiwi 直接查 `max_sector_stopovers=0`，
**TPE→OOL 整段 2027-06-05~06-16 查到 0 筆結果**——不是解析問題，是這條航線
本來就沒有直飛。所以目前 `routes.yaml` 追的是布里斯本(BNE) 取代黃金海岸：
天天有 China Airlines / EVA Air 直飛，開車約一小時可到黃金海岸。想要 OOL
也一起比、接受轉機的話，`routes.yaml` 檔尾有寫法備忘，或用 `/stops` `/to`
從指令頻道加回來。

**只要直飛（`max_stops: 0`）會讓某些航線更常查無資料**，這是取捨後的結果，
不是 bug：轉機選項通常比直飛多，關掉轉機等於縮小候選池。多段行程那條
（雪梨進、布里斯本出）目前回程日期 `2027-06-16` 查證當下沒查到直飛班次，
但前後幾天幾乎每天都有——那個日期是 2026-09 查的，離出發還有 9 個月，
航空公司很可能還沒把整個班表排出來，不一定是真的停飛，`routes.yaml` 裡
已經寫了這個保留意見。

> ⚠️ 注意：一整個目的地全數失敗時，Actions 仍然是綠的，
> 因為只要還有別的路線查到價格就不算整輪失敗。
> 要確認某個目的地有沒有真的抓到資料，看 `data/prices.csv` 裡有沒有它的列。

---

## 排程的可靠度

GitHub 的排程是 **best-effort**，不保證準時，這是它的已知限制，不是設定錯誤：

- **會延遲**，尤其整點前後（全球負載尖峰），偶爾延遲數十分鐘到數小時。
  所以兩個 workflow 都刻意避開整點（`23 1 * * *`、`13,43 * * * *`）。
- **負載夠高時會直接丟掉某幾次執行**。每天查價漏一次無所謂；
  指令頻道則是靠「輪詢頻率遠密於 Telegram／ntfy 的訊息保留窗口」來吸收，漏跑一兩次不會讓指令消失。
- **公開 repo 連續 60 天沒有新 commit，排程會被自動停用**。
  這個追蹤器每天都會 commit 價格紀錄，所以正常情況不會觸發。
  但要注意這是連鎖的：如果追蹤器壞掉不再 commit，60 天後排程也會被關掉——
  上面那則「整輪失敗」通知就是為了讓你在那之前就知道。

急著要結果時，任何 workflow 都可以在 Actions 頁面按 **Run workflow** 立刻觸發。

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
