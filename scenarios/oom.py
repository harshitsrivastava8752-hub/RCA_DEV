import os
import sys
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

NS = "demo-app"
SELECTOR = "app=checkout-api"
ROUNDS = 3
LEAK_MB = 400
TRAFFIC_POD = "rca-traffic"
TRAFFIC_CODE = (
    "import time, urllib.request\n"
    "while True:\n"
    "    try:\n"
    "        urllib.request.urlopen(urllib.request.Request('http://frontend:8080/checkout', data=b'', method='POST'), timeout=5).read()\n"
    "    except Exception:\n"
    "        pass\n"
    "    time.sleep(0.5)\n"
)


def start_traffic(core):
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=TRAFFIC_POD, namespace=NS, labels={"app": TRAFFIC_POD}),
        spec=client.V1PodSpec(
            restart_policy="Always",
            termination_grace_period_seconds=0,
            containers=[
                client.V1Container(
                    name="traffic",
                    image="rca-demo/frontend:v1",
                    image_pull_policy="IfNotPresent",
                    command=["python", "-u", "-c", TRAFFIC_CODE],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "5m", "memory": "16Mi"}, limits={"memory": "64Mi"}
                    ),
                )
            ],
        ),
    )
    try:
        core.create_namespaced_pod(NS, pod)
    except ApiException as e:
        if e.status != 409:
            raise
    print(f"traffic generator running as pod {TRAFFIC_POD} in {NS}")


def stop_traffic(core):
    try:
        core.delete_namespaced_pod(TRAFFIC_POD, NS)
    except ApiException as e:
        if e.status != 404:
            raise


def ready_pods(core):
    names = []
    for p in core.list_namespaced_pod(NS, label_selector=SELECTOR).items:
        if p.metadata.deletion_timestamp or p.status.phase != "Running":
            continue
        cs = p.status.container_statuses or []
        if cs and all(c.ready for c in cs):
            names.append(p.metadata.name)
    return names


def wait_ready(core, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        names = ready_pods(core)
        if names:
            return names
        time.sleep(3)
    raise TimeoutError(f"no Ready pod for {SELECTOR} within {timeout}s")


def restarts(pod):
    return sum(c.restart_count for c in (pod.status.container_statuses or []))


def wait_restart(core, name, before, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pod = core.read_namespaced_pod(name, NS)
        cs = pod.status.container_statuses or []
        if restarts(pod) > before and cs and all(c.ready for c in cs):
            return
        time.sleep(3)
    raise TimeoutError(f"{name} did not restart and become Ready within {timeout}s")


def leak(core, name):
    cmd = [
        "python",
        "-c",
        f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/debug/leak?mb={LEAK_MB}', timeout=60).read()",
    ]
    try:
        out = stream(
            core.connect_get_namespaced_pod_exec,
            name,
            NS,
            command=cmd,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        if out:
            print(f"  exec output: {out[:300]}")
    except Exception as e:
        print(f"  exec ended with {type(e).__name__}")


def apply(core):
    start_traffic(core)
    for i in range(ROUNDS):
        for name in wait_ready(core):
            before = restarts(core.read_namespaced_pod(name, NS))
            print(f"round {i + 1}/{ROUNDS}: leaking {LEAK_MB}MB in {name}")
            leak(core, name)
            wait_restart(core, name, before)
    print(f"done: checkout-api pods restarted {ROUNDS} time(s) after exceeding the memory limit")


def revert(core):
    core.delete_collection_namespaced_pod(NS, label_selector=SELECTOR)
    wait_ready(core)
    stop_traffic(core)
    print("reverted: checkout-api pods recreated, traffic generator removed")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "revert"):
        sys.exit("usage: python scenarios/oom.py apply|revert")
    config.load_kube_config(context=os.environ.get("KUBE_CONTEXT", "kind-rca-agent"))
    core = client.CoreV1Api()
    (apply if sys.argv[1] == "apply" else revert)(core)


if __name__ == "__main__":
    main()
