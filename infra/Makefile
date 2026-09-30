CLUSTER ?= rca-agent
CTX := kind-$(CLUSTER)
INFRA := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
KUBECTL := kubectl --context $(CTX)
HELM := helm --kube-context $(CTX)
HELM_INSTALL := $(HELM) install -n observability --wait --timeout 10m

.PHONY: up down status

up:
	kind create cluster --name $(CLUSTER) --config $(INFRA)/kind-config.yaml --wait 120s
	$(KUBECTL) apply -f $(INFRA)/manifests/namespaces.yaml
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo add grafana https://grafana.github.io/helm-charts
	helm repo add jaegertracing https://jaegertracing.github.io/helm-charts
	helm repo update
	$(HELM_INSTALL) prometheus prometheus-community/kube-prometheus-stack -f $(INFRA)/helm-values/kube-prometheus-stack.yaml
	$(HELM_INSTALL) loki grafana/loki-stack -f $(INFRA)/helm-values/loki-stack.yaml
	$(HELM_INSTALL) jaeger jaegertracing/jaeger -f $(INFRA)/helm-values/jaeger.yaml
	$(KUBECTL) apply -f $(INFRA)/manifests/rbac.yaml
	$(KUBECTL) apply -f $(INFRA)/manifests/networkpolicy.yaml

down:
	kind delete cluster --name $(CLUSTER)

status:
	$(KUBECTL) get pods -A
