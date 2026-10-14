"""
Generates synthetic AWS billing data (simplified format inspired by the AWS CUR / FOCUS)
for the FinOps Excel dashboard.

Output:
  data/raw/billing_2026-01.csv ... billing_2026-06.csv   (one file per month, like a real export)
  data/ref/accounts.csv                                   (account -> default team)
  data/ref/budget.csv                                     (budget per team/month)

Stories planted in the data (the dashboard should reveal them):
  1. Team "data" starts 3 GPU-sized instances in a dev environment on 10 Apr and never stops them.
  2. CloudFront spike for team "web" between 12 and 18 May (campaign).
  3. Untagged cost concentrated in the sandbox account.

Run from the repository root:  python scripts/generate_billing.py
"""
import csv
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

random.seed(42)  # same data on every run

MONTHS = [(2026, m) for m in range(1, 7)]
OUT_RAW = Path("data/raw")
OUT_REF = Path("data/ref")

# account_id, account_name, default_team (used to allocate untagged cost)
ACCOUNTS = [
    ("111111111111", "prod-platform", "platform"),
    ("222222222222", "prod-data", "data"),
    ("333333333333", "prod-web", "web"),
    ("444444444444", "sandbox", "platform"),
]
TEAMS = ["platform", "data", "web"]

# service -> (usage unit, daily cost range per resource in USD)
SERVICES = {
    "AmazonEC2":        ("Hrs",      (8, 40)),
    "AmazonRDS":        ("Hrs",      (10, 30)),
    "AmazonS3":         ("GB-Mo",    (1, 6)),
    "AWSLambda":        ("Requests", (0.5, 4)),
    "AmazonCloudFront": ("GB",       (2, 10)),
    "AmazonDynamoDB":   ("WCU-Hrs",  (1, 8)),
}
PREFIX = {
    "AmazonEC2": "i-", "AmazonRDS": "db-", "AmazonS3": "bucket-",
    "AWSLambda": "fn-", "AmazonCloudFront": "dist-", "AmazonDynamoDB": "ddb-",
}
REGIONS = ["us-east-1", "sa-east-1", "eu-west-1"]
ENVS = ["prod", "staging", "dev"]

FIELDS = [
    "usage_date", "account_id", "account_name", "service", "region",
    "resource_id", "usage_amount", "usage_unit", "cost_usd",
    "tag_team", "tag_environment",
]

MONTHLY_GROWTH = 1.03  # 3% organic growth per month


def hexid(n):
    return "".join(random.choices("0123456789abcdef", k=n))


def make_resource(account, service, team, env, region=None, base=None, start=None):
    account_id, account_name, _ = account
    unit, (lo, hi) = SERVICES[service]
    return {
        "account_id": account_id,
        "account_name": account_name,
        "service": service,
        "region": region or random.choice(REGIONS),
        "resource_id": PREFIX[service] + hexid(12),
        "unit": unit,
        "unit_price": random.uniform(0.02, 0.5),
        "base": base if base is not None else random.uniform(lo, hi),
        "team": team,
        "env": env,
        "start": start or date(2026, 1, 1),
    }


def build_resources():
    resources = []
    for account in ACCOUNTS:
        is_sandbox = account[1] == "sandbox"
        for _ in range(6 if is_sandbox else 15):
            service = random.choice(list(SERVICES))
            env = "dev" if is_sandbox else random.choices(ENVS, weights=[6, 2, 2])[0]
            untagged_prob = 0.6 if is_sandbox else 0.08
            if random.random() < untagged_prob:
                team = ""
            else:
                team = random.choice(TEAMS) if is_sandbox else account[2]
            resources.append(make_resource(account, service, team, env))

    # Make sure team web has a CloudFront distribution (needed for story 2)
    resources.append(make_resource(ACCOUNTS[2], "AmazonCloudFront", "web", "prod",
                                   region="us-east-1", base=8))

    # Story 1: forgotten GPU-sized instances of team data, running from 10 Apr
    for _ in range(3):
        resources.append(make_resource(ACCOUNTS[1], "AmazonEC2", "data", "dev",
                                       region="us-east-1",
                                       base=random.uniform(70, 95),
                                       start=date(2026, 4, 10)))
    return resources


def daily_cost(r, d):
    cost = r["base"] * (MONTHLY_GROWTH ** (d.month - 1)) * random.uniform(0.85, 1.15)
    # Story 2: CloudFront spike for team web
    if (r["service"] == "AmazonCloudFront" and r["account_name"] == "prod-web"
            and date(2026, 5, 12) <= d <= date(2026, 5, 18)):
        cost *= 6
    return cost


def month_days(y, m):
    d = date(y, m, 1)
    while d.month == m:
        yield d
        d += timedelta(days=1)


def main():
    OUT_RAW.mkdir(parents=True, exist_ok=True)
    OUT_REF.mkdir(parents=True, exist_ok=True)

    resources = build_resources()
    default_team = {a[0]: a[2] for a in ACCOUNTS}
    jan_cost_by_team = defaultdict(float)

    for y, m in MONTHS:
        path = OUT_RAW / f"billing_{y}-{m:02d}.csv"
        rows = 0
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            for d in month_days(y, m):
                for r in resources:
                    if d < r["start"]:
                        continue
                    cost = daily_cost(r, d)
                    w.writerow({
                        "usage_date": d.isoformat(),
                        "account_id": r["account_id"],
                        "account_name": r["account_name"],
                        "service": r["service"],
                        "region": r["region"],
                        "resource_id": r["resource_id"],
                        "usage_amount": round(cost / r["unit_price"], 4),
                        "usage_unit": r["unit"],
                        "cost_usd": round(cost, 4),
                        "tag_team": r["team"],
                        "tag_environment": r["env"],
                    })
                    rows += 1
                    if m == 1:
                        jan_cost_by_team[r["team"] or default_team[r["account_id"]]] += cost
        print(f"{path}: {rows} rows")

    with (OUT_REF / "accounts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["account_id", "account_name", "default_team"])
        w.writerows(ACCOUNTS)

    # Budget: January daily average, expected growth + 10% headroom
    with (OUT_REF / "budget.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "team", "budget_usd"])
        for y, m in MONTHS:
            days = sum(1 for _ in month_days(y, m))
            for team in TEAMS:
                daily = jan_cost_by_team[team] / 31
                budget = daily * days * (MONTHLY_GROWTH ** (m - 1)) * 1.10
                w.writerow([f"{y}-{m:02d}", team, round(budget, -2)])
    print("data/ref/accounts.csv and data/ref/budget.csv written")


if __name__ == "__main__":
    main()
