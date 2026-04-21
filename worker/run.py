"""
Standalone check worker — runs on VPS, polls for jobs, dispatches to the right
checker based on monitor type, and reports results back to the API.

Usage:
    python run.py --region us --api-url http://your-api:8000 --secret your-worker-secret
    # or via env vars: REGION, API_URL, WORKER_SECRET
"""

import argparse
import concurrent.futures
import os
import platform
import time

import httpx

from checkers import get_checker


# Hard ceiling = timeout_seconds * 2 (covers retry inside the checker) + slack
# for shutdown. If the checker is still running past this, the thread is
# orphaned and the lease will expire server-side, letting another worker reclaim.
HARD_TIMEOUT_SLACK_SECONDS = 10


def get_config():
    parser = argparse.ArgumentParser(description="CheckPulse check worker")
    parser.add_argument("--region", default=os.getenv("REGION", "us"))
    parser.add_argument("--api-url", default=os.getenv("API_URL", "http://localhost:8000"))
    parser.add_argument("--secret", default=os.getenv("WORKER_SECRET", "change-me-worker-secret"))
    parser.add_argument("--poll-interval", type=int, default=int(os.getenv("POLL_INTERVAL", "10")))
    parser.add_argument("--worker-id", default=os.getenv("WORKER_ID", platform.node() or "worker"))
    return parser.parse_args()


def auth_headers(secret: str, worker_id: str) -> dict:
    return {"X-Worker-Secret": secret, "X-Worker-ID": worker_id}


def fetch_jobs(client: httpx.Client, api_url: str, region: str, secret: str, worker_id: str) -> list[dict]:
    resp = client.get(
        f"{api_url}/internal/jobs",
        params={"region": region},
        headers=auth_headers(secret, worker_id),
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
    hard_timeout = int(job.get("timeout_seconds") or 10) * 2 + HARD_TIMEOUT_SLACK_SECONDS

    try:
        checker = get_checker(monitor_type)
    except ValueError as e:
        result["status"] = "down"
        result["error"] = str(e)[:500]
        return result

    # Run the check in a worker thread with a hard ceiling. If it overruns, we
    # orphan the thread (shutdown(wait=False)) and move on — the next poll will
    # re-claim any still-leased pending_check after its lease expires.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(checker.run, job, client)
        try:
            check_result = future.result(timeout=hard_timeout)
            result.update(check_result.to_payload())
        except concurrent.futures.TimeoutError:
            result["status"] = "down"
            result["error"] = f"Hard timeout after {hard_timeout}s"
        except Exception as e:
            result["status"] = "down"
            result["error"] = f"Checker error: {str(e)[:480]}"
    finally:
        pool.shutdown(wait=False)
    return result


def post_results(client: httpx.Client, api_url: str, secret: str, worker_id: str, results: list[dict]):
    resp = client.post(
        f"{api_url}/internal/results",
        json={"results": results},
        headers=auth_headers(secret, worker_id),
    )
    resp.raise_for_status()
    return resp.json()


def send_heartbeat(client: httpx.Client, api_url: str, region: str, secret: str, worker_id: str):
    try:
        client.post(
            f"{api_url}/internal/heartbeat",
            params={"region": region, "hostname": platform.node(), "version": "1.0.0"},
            headers=auth_headers(secret, worker_id),
            timeout=5,
        )
    except Exception:
        pass  # Heartbeat failure is non-fatal


def main():
    config = get_config()
    print(f"Worker starting: id={config.worker_id} region={config.region} api={config.api_url} poll={config.poll_interval}s")

    heartbeat_counter = 0
    with httpx.Client() as client:
        while True:
            try:
                heartbeat_counter += 1
                if heartbeat_counter >= 6:
                    send_heartbeat(client, config.api_url, config.region, config.secret, config.worker_id)
                    heartbeat_counter = 0

                jobs = fetch_jobs(client, config.api_url, config.region, config.secret, config.worker_id)
                if jobs:
                    print(f"Got {len(jobs)} job(s)")
                    results = [execute_check(client, job) for job in jobs]
                    resp = post_results(client, config.api_url, config.secret, config.worker_id, results)
                    print(f"Posted {resp.get('saved', 0)} result(s)")
                    for r in results:
                        status_icon = "+" if r["status"] == "up" else "!"
                        print(f"  [{status_icon}] {r['monitor_id'][:8]}... {r['status']} {r.get('response_time_ms', '-')}ms")
            except Exception as e:
                print(f"Error: {e}")

            time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
