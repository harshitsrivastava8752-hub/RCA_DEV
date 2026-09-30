import os
import sys
import time
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NS = "demo-app"
DEPLOY = "checkout-api"
CONFIGMAP = "checkout-api-config"
GOOD_URL = "postgresql://app:app_password@postgres:5432/orders"
BAD_URL = "postgresql://app:app_password@postgres-db:5432/orders"
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


def wait_rollout(apps, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = apps.read_namespaced_deployment(DEPLOY, NS)
        s = d.status
        want = d.spec.replicas
        if (
            (s.observed_generation or 0) >= d.metadata.generation
            and (s.updated_replicas or 0) == want
            and (s.replicas or 0) == want
            and (s.available_replicas or 0) == want
        ):
            return
        time.sleep(3)
    raise TimeoutError(f"rollout of {DEPLOY} did not finish within {timeout}s")


def set_url_and_restart(core, apps, url):
    core.patch_namespaced_config_map(CONFIGMAP, NS, {"data": {"DATABASE_URL": url}})
    restarted_at = datetime.now(timezone.utc).isoformat()
    patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": restarted_at}}}}}
    apps.patch_namespaced_deployment(DEPLOY, NS, patch)
    wait_rollout(apps)


def apply(core, apps):
    start_traffic(core)
    set_url_and_restart(core, apps, BAD_URL)
    print("done: checkout-api-config DATABASE_URL points at a nonexistent host; checkout-api restarted")


def revert(core, apps):
    set_url_and_restart(core, apps, GOOD_URL)
    stop_traffic(core)
    print("reverted: DATABASE_URL restored, checkout-api restarted, traffic generator removed")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "revert"):
        sys.exit("usage: python scenarios/config_problem.py apply|revert")
    config.load_kube_config(context=os.environ.get("KUBE_CONTEXT", "kind-rca-agent"))
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    (apply if sys.argv[1] == "apply" else revert)(core, apps)


if __name__ == "__main__":
    main()
