"""
Standalone check worker — runs on VPS, polls for jobs, dispatches to the right
checker based on monitor type, and reports results back to the API.

Usage:
    python run.py --region us --api-url http://your-api:8000 --secret your-worker-secret
    # or via env vars: REGION, API_URL, WORKER_SECRET
"""

import argparse
import os
import platform
import time

import httpx

from checkers import get_checker


def get_config():
    parser = argparse.ArgumentParser(description="CheckPulse check worker")
    parser.add_argument("--region", default=os.getenv("REGION", "us"))
    parser.add_argument("--api-url", default=os.getenv("API_URL", "http://localhost:8000"))
    parser.add_argument("--secret", default=os.getenv("WORKER_SECRET", "change-me-worker-secret"))
    parser.add_argument("--poll-interval", type=int, default=int(os.getenv("POLL_INTERVAL", "10")))
    return parser.parse_args()


def fetch_jobs(client: httpx.Client, api_url: str, region: str, secret: str) -> list[dict]:
    resp = client.get(
        f"{api_url}/internal/jobs",
        params={"region": region},
        headers={"X-Worker-Secret": secret},
    )
    resp.raise_for_status()
    return resp.json().get("jobs", [])


def execute_check(client: httpx.Client, job: dict) -> dict:
    result = {
        "pending_check_id": job["pending_check_id"],
        "monitor_id": job["monitor_id"],
        "region": job["region"],
    }
    monitor_type = job.get("type", "http")
    try:
        checker = get_checker(monitor_type)
        check_result = checker.run(job, client)
        result.update(check_result.to_payload())
    except ValueError as e:
        result["status"] = "down"
        result["error"] = str(e)[:500]
    except Exception as e:
        result["status"] = "down"
        result["error"] = f"Checker error: {str(e)[:480]}"
    return result


def post_results(client: httpx.Client, api_url: str, secret: str, results: list[dict]):
    resp = client.post(
        f"{api_url}/internal/results",
        json={"results": results},
        headers={"X-Worker-Secret": secret},
    )
    resp.raise_for_status()
    return resp.json()


def send_heartbeat(client: httpx.Client, api_url: str, region: str, secret: str):
    try:
        client.post(
            f"{api_url}/internal/heartbeat",
            params={"region": region, "hostname": platform.node(), "version": "1.0.0"},
            headers={"X-Worker-Secret": secret},
            timeout=5,
        )
    except Exception:
        pass  # Heartbeat failure is non-fatal


def main():
    config = get_config()
    print(f"Worker starting: region={config.region} api={config.api_url} poll={config.poll_interval}s")

    heartbeat_counter = 0
    with httpx.Client() as client:
        while True:
            try:
                heartbeat_counter += 1
                if heartbeat_counter >= 6:
                    send_heartbeat(client, config.api_url, config.region, config.secret)
                    heartbeat_counter = 0

                jobs = fetch_jobs(client, config.api_url, config.region, config.secret)
                if jobs:
                    print(f"Got {len(jobs)} job(s)")
                    results = [execute_check(client, job) for job in jobs]
                    resp = post_results(client, config.api_url, config.secret, results)
                    print(f"Posted {resp.get('saved', 0)} result(s)")
                    for r in results:
                        status_icon = "+" if r["status"] == "up" else "!"
                        print(f"  [{status_icon}] {r['monitor_id'][:8]}... {r['status']} {r.get('response_time_ms', '-')}ms")
            except Exception as e:
                print(f"Error: {e}")

            time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
