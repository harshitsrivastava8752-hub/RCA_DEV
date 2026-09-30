import os
import sys
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NS = "demo-app"
DEPLOY = "postgres"
SELECTOR = "app=postgres"
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


def scale(apps, replicas):
    apps.patch_namespaced_deployment_scale(DEPLOY, NS, {"spec": {"replicas": replicas}})


def wait_gone(core, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not core.list_namespaced_pod(NS, label_selector=SELECTOR).items:
            return
        time.sleep(2)
    raise TimeoutError(f"{DEPLOY} pods still present after {timeout}s")


def wait_ready(core, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for p in core.list_namespaced_pod(NS, label_selector=SELECTOR).items:
            cs = p.status.container_statuses or []
            if not p.metadata.deletion_timestamp and p.status.phase == "Running" and cs and all(c.ready for c in cs):
                return
        time.sleep(3)
    raise TimeoutError(f"no Ready pod for {SELECTOR} within {timeout}s")


def apply(core, apps):
    start_traffic(core)
    scale(apps, 0)
    wait_gone(core)
    print("done: postgres scaled to 0; checkout-api can no longer reach its database")


def revert(core, apps):
    scale(apps, 1)
    wait_ready(core)
    stop_traffic(core)
    print("reverted: postgres scaled to 1, traffic generator removed")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "revert"):
        sys.exit("usage: python scenarios/dependency_failure.py apply|revert")
    config.load_kube_config(context=os.environ.get("KUBE_CONTEXT", "kind-rca-agent"))
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    (apply if sys.argv[1] == "apply" else revert)(core, apps)


if __name__ == "__main__":
    main()
