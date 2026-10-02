# Automation flow

How the scripts hand work to each other, every decision they take on their
own, and the places where the chain still stops and waits for a person.
Everything here was read from the code (and the Task Scheduler settings), not
from memory.

`Daily_Envs.txt` is the shared state: every script reads it and most of them
write to it. Each block is one build, each line one environment, and the
status header an env sits under (`PASSED`, `IN PROGRESS`, `ON HOLD`,
`FAILED`, `BLOCKED`) is what the next script acts on.

## Daily deployment cycle (NTT)

```mermaid
flowchart TD
    classDef script fill:#e8f0fe,stroke:#3b6fd8,color:#111
    classDef human fill:#fff4d6,stroke:#c99400,color:#111
    classDef store fill:#eef7ee,stroke:#3d8b3d,color:#111
    classDef gap fill:#fde8e8,stroke:#c0392b,color:#111,stroke-dasharray:5 3
    classDef ext fill:#f2f2f2,stroke:#888,color:#111

    MAIL(["Build mail in Outlook"]):::human --> LE["List_Envs.py"]:::script
    LE -->|"r / v"| DE[("Daily_Envs.txt")]:::store
    LE --> OR

    DE -. "new envs have no status header" .-> HOLD{{"lands under ON HOLD"}}
    HOLD --> G1["GAP 1: nothing triggers the first deployment.<br/>You move it to IN PROGRESS and run QA/COM_Envs"]:::gap

    subgraph TRIGGER ["Trigger deployments"]
        QA["QA_Envs.py (nttcw)"]:::script
        COM["COM_Envs.py (nttcom)"]:::script
        QA --> CLS{"Last build in Jenkins?"}
        COM --> CLS
        CLS -->|"running"| SKIP1["leave it"]
        CLS -->|"today, under 6 h"| SKIP2["leave it"]
        CLS -->|"not today"| PAY
        CLS -->|"today 6 h+ / no date"| AUTO{"Full auto?"}
        AUTO -->|"QA_Envs: yes"| PAY
        AUTO -->|"COM_Envs or --ask"| ASK1[/"asks you"/]:::human
        ASK1 --> PAY
        PAY{"SD / snapshot trustworthy?"}
        PAY -->|"ambiguous or not .yaml"| BLK["skip + report"]
        PAY -->|"snapshot"| TEN{"Tenant?"}
        TEN -->|"COM: from _T n"| JF
        TEN -->|"QA: QA_ENVS_TENANT"| JF
        TEN -->|"missing"| BLK
        PAY -->|"SD file"| JF["Jenkins form: SD_FILE, CLEANUP_FS,<br/>STOP_APPS_BEFORE, START_APPS, Build"]
    end
    JF --> JEN[("Jenkins: Deploy-Parallel /<br/>Restore-Eatlon-SnapShot")]:::ext

    SCHED[["Task Scheduler<br/>Mon-Fri 15:00-23:00, every 10 min"]]:::ext --> TD
    subgraph TRACK ["Track_Deployments.py"]
        TD["clean zombie Python and orphan Chromium"]:::script --> RD["IN PROGRESS envs without a failure reason"]
        RD --> API["status via Jenkins API, 8 at a time"]
        API -->|"login rejected"| LOGIN["stop, touch nothing"]
        LOGIN --> G8["GAP 8: expired password only goes to the log"]:::gap
        API --> ST{"Status?"}
        ST -->|"IN PROGRESS"| STK{"Stage stuck?<br/>Stop/Start over 30 min, others over 2 h"}
        STK -->|"yes"| G4["GAP 4: flagged in the log only"]:::gap
        STK -->|"no"| WAIT["check again in 10 min"]
        ST -->|"ABORTED"| AB{"Someone at the terminal?"}
        AB -->|"yes"| ABQ[/"pass to PASSED?"/]:::human
        AB -->|"no"| G5["GAP 5: stays IN PROGRESS forever"]:::gap
        ST -->|"not today / no date"| G6["GAP 6: ignored, nobody retriggers"]:::gap
        ST -->|"FAILED"| RS{"Restore-Eatlon-SnapShot?"}
        RS -->|"yes"| TEN2["write 'possible wrong tenant'"]
        TEN2 --> G3["GAP 3: Deployment_Defect is not launched for this"]:::gap
        RS -->|"no"| CHR{"Same stages failed 3+ times today?<br/>(Check and Clean Up SHARED never counts)"}
        CHR -->|"yes"| WF["write the failure reason"]
        CHR -->|"no"| REB{"Rebuild?<br/>unattended default: yes"}
        REB -->|"yes"| RBLD["Rebuild, unticking what already passed"]
        REB -->|"no"| WF
        WF --> DDL["launch Deployment_Defect"]
        ST -->|"PASSED, COM"| MPC["move to PASSED"]
        MPC --> ALLC{"All COM PASSED?"}
        ALLC -->|"yes"| SCOM["launch Sanity_COM"]
        ST -->|"PASSED, cw"| UN{"Someone at the terminal?"}
        UN -->|"no"| PEND["append envs to pending_sanity_envs.txt<br/>+ schtasks 'E2E Sanity'"]
        UN -->|"yes"| SQ[/"run sanity?"/]:::human
    end
    JEN --> API
    RBLD --> JEN

    PEND --> E2E
    SQ -->|"y"| E2E
    SQ -->|"n"| MKP[/"mark PASSED?"/]:::human
    PEND -. "task already running" .-> G7["GAP 7: E2E never exits and the task is IgnoreNew,<br/>so new envs wait in the queue up to 4 h"]:::gap

    subgraph SANITY ["E2E sanity"]
        E2E["E2E_Flow1_Till_Billing.py<br/>SAML once, EAST + WEST per env, 3 at a time"]:::script
        E2E --> ACC{"Account number shows up?"}
        ACC -->|"yes"| WACC["write EAST / WEST to Daily_Envs right away"]
        ACC -->|"3 min without it"| RR["click Rerun, max 2 per tab"]
        ACC -->|"3 min without progress, or 10 min"| GO["stop waiting"]
        WACC --> RES
        GO --> RES["Results_Sanity.py"]:::script
        RES --> ORS["OrganizeResults"]
        RES --> RER["Rerun_Sanity.py"]:::script
        RER --> G9["GAP 9: nobody collects the accounts after these reruns"]:::gap
        RES --> CLOSE["close tabs of envs that just PASSED"]
    end

    subgraph ORGANIZE ["OrganizeResults.py"]
        OR["back up, rebuild every block"]:::script --> RULES{"Per env"}
        RULES -->|"ON HOLD / BLOCKED"| STAY["stays: those are yours"]
        RULES -->|"has '- reason'"| FAILEDS["FAILED"]
        RULES -->|"has EAST + WEST"| PASSEDS["PASSED"]
        RULES -->|"ON HOLD, no reason, block has a PASSED"| PROMO["IN PROGRESS"]
        PROMO --> QAT["launch QA_Envs"]
        QAT --> G2["GAP 2: promoted COM envs also go to QA_Envs,<br/>which ignores COM"]:::gap
        OR --> WXQ{"run_webex?"}
        WXQ -->|"yes"| WX
    end
    ORS --> OR

    subgraph DEFECT ["Deployment_Defect.py"]
        DDL --> DD["OrganizeResults without Webex"]:::script
        DD --> FL["FAILED lines without a Jira link"]
        FL --> LOG["failed build: console, consoleFull, consoleText"]
        LOG --> EXT["cut the error block<br/>SQL errors to DevOps, the rest to IT"]
        EXT --> GRP["merge envs with the same error<br/>same component and patch, similarity 0.92+"]
        GRP --> CONF[/"create these tickets? Y/n"/]:::human
        CONF --> G10["GAP 10: waits forever if nobody answers;<br/>later triggers are ignored for up to 8 h"]:::gap
        CONF -->|"Y"| JIRA[("Jira Defect")]:::ext
        JIRA --> LNK["paste the link on the Daily_Envs line"]
        LNK --> WX
    end

    WX["Webex_Update.py<br/>edit today's messages, post new blocks"]:::script --> ROOM[("Webex room")]:::ext
    WX -. "token expired" .-> G11["GAP 11: fails silently inside the other scripts"]:::gap

    SCOM --> SCR["Sanity_COM.py<br/>headless Chrome, SAML, one tab per env"]:::script
    SCR --> G12["GAP 12: only prints the execution links;<br/>no pass/fail, nothing written back"]:::gap

    DE --> MFB["MailQAs.py"]:::script
    MFB --> XL[("QG Scope Analysis.xlsx")]:::store
    MFB --> OUT[("reply-all in Outlook")]:::ext
    MFB --> DEL["delete the mailed block from Daily_Envs"]
    DEL --> G13["GAP 13: no backup of the deleted block"]:::gap
```

### Manual tools around the cycle

| Script | What it does | How it's started |
|---|---|---|
| `TS_Sanity.py` | Opens the two PuTTY tunnels, launches Chrome through the SOCKS proxy and runs the TS sanity scope | by hand |
| `SBA.py` / `Divided_SBA.py` | Triggers SBA test builds / uploads split SBA files, collects the report links | by hand |
| `NTTTESTs_Resolve.py` | Resolves a pasted list of NTTTESTS tickets, 5 at a time | by hand |
| `POTs_Ext_Sanities.py` | Prepares (never publishes) the Confluence copies for external sanities | by hand |
| `Ext_Sanities_Screenshots.py` | Alt+S driven screenshot pasting into the sanity sheet | by hand |
| `Setup_Credentials.py` | Stores / lists / deletes the encrypted credentials | by hand |

## GCI validation reports

```mermaid
flowchart LR
    classDef script fill:#e8f0fe,stroke:#3b6fd8,color:#111
    classDef human fill:#fff4d6,stroke:#c99400,color:#111
    classDef ext fill:#f2f2f2,stroke:#888,color:#111

    IN(["Server Details + TOMS + ticket links"]):::human --> PIM["PIM_Validation.py<br/>offerings, price nodes, parameters"]:::script
    IN --> AM["AM_Validation.py<br/>Stock Item Models"]:::script
    PIM --> DOC[("Downloads/TICKET.docx")]
    AM --> DOC
    DOC --> XS["Excel_Screenshots.py<br/>loader rows, walking right"]:::script
    XS --> OK{"All good?"}:::human
    OK -->|"yes"| J[("Jira: attach, comment 'Tested And Passed',<br/>move one step")]:::ext
    OK -->|"no"| STOP["nothing is sent"]
```

`GCIBilling.py` writes the billing run-book (FDE / IREP / RRDEP commands) for a
set of accounts; `AutomaticScreenshots.py` is the old Alt+S-to-Word helper;
`filter_probe.py` is a read-only diagnostic for the TOMS filter.

## Gaps — where the chain still waits for a person

| # | Where | What happens today | Idea |
|---|---|---|---|
| 1 | `List_Envs` → `OrganizeResults` | New envs have no status header, so they land under **ON HOLD**. Nothing triggers their first deployment. | Let `List_Envs` write them under IN PROGRESS when the SD file is already in the mail, and call `QA_Envs` / `COM_Envs`. |
| 2 | `OrganizeResults` | ON HOLD → IN PROGRESS promotions always launch `QA_Envs`, which ignores COM envs. Promoted COM envs are never triggered. | Launch `COM_Envs` too when a promoted line is COM (it needs a non-interactive mode first). |
| 3 | `Track_Deployments` | A failed Restore-Eatlon-SnapShot is written as "possible wrong tenant", but `Deployment_Defect` is only launched for other failures. | Decide whether a tenant problem deserves a ticket; if not, at least post it to Webex. |
| 4 | `Track_Deployments` | Stuck stages (Stop/Start > 30 min, others > 2 h) are only flagged in the log. | Post a Webex alert once per stuck build. |
| 5 | `Track_Deployments` | In unattended runs an ABORTED build stays IN PROGRESS and is re-checked every 10 min forever. | After N runs, move it to ON HOLD with a reason. |
| 6 | `Track_Deployments` | Builds that aren't from today (or have no date) are ignored, and nobody retriggers them. | Hand them to `QA_Envs` / `COM_Envs` (they already know how to decide). |
| 7 | `E2E Sanity` task | E2E sleeps forever at the end to keep the browser open; the task policy is *IgnoreNew* with a 4 h limit, so new PASSED envs wait in `pending_sanity_envs.txt` for up to 4 h. | Exit once the accounts are written (or watch the pending file while sleeping). |
| 8 | Unattended `Track_Deployments` | A rejected password stops everything but only shows in the log. | Post to Webex: "run Setup_Credentials". |
| 9 | `Results_Sanity` → `Rerun_Sanity` | Reruns are fired and the script exits; nobody reads the accounts that those reruns produce. | Poll the rerun tabs for a few minutes, or run `Results_Sanity` again after the reruns. |
| 10 | `Deployment_Defect` | Waits for "create these tickets? [Y/n]" with no timeout; later triggers are ignored for up to 8 h meanwhile. | Time out to a Webex summary, or auto-create for well-known errors (Check & Clean Up, SQL). |
| 11 | `Webex_Update` | If the token can't be refreshed it exits with an error that the calling script only prints. | Fall back to a local notification / mail. |
| 12 | `Sanity_COM` | Launches the runs and prints the links; pass/fail is never collected or written back. | Poll the execution pages like E2E does and write the result to Daily_Envs. |
| 13 | `MailQAs` | Deletes the mailed block from `Daily_Envs.txt` without a backup (the other writers do keep one). | Copy the file to `.bak` first, like `OrganizeResults`. |

Also worth a look: `SBA.py` writes to `Documents\QG Scope Analysis.xlsx` while
`MailQAs.py` uses `Documents\NTT\QG Scope Analysis.xlsx`. It was already
like that; if it isn't on purpose, one of them is writing to a stale copy.
