In order to write the report for this project we made use of "prometheus" for monitoring the application's container.

Guide to install:

  1) Install helm:
    - choco install kubernetes-helm

  4) Add helm repository prometheus:
	  - helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	  - helm repo update

Guide to use:

  1) Install prometheus (This line generates deploynment/pods and everything):

     - helm install prometheus prometheus-community/kube-prometheus-stack


  3) Access Grafana UI: 
	  - kubectl port-forward deployment/prometheus-grafana 3000
	  - (Navegator) http://localhost:3000/login  -> user: admin  pass: prom-operator

  4) Access Prometheus UI:
	  - kubectl port-forward prometheus-prometheus-kube-prometheus-prometheus-0 9090
	  - (Navegator) http://localhost:9090/
