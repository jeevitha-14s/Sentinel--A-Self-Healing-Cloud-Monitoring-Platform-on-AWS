# Sentinel – Self-Healing Cloud Monitoring Platform

> **A production-inspired AWS platform that automatically detects application failures, restarts unhealthy services without SSH, and alerts operators—all built using Infrastructure as Code.**

![Python](https://img.shields.io/badge/Python-3.12-blue)
![AWS](https://img.shields.io/badge/AWS-Cloud-orange)
![Terraform](https://img.shields.io/badge/Terraform-IaC-7B42BC)
![Docker](https://img.shields.io/badge/Docker-Container-2496ED)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-CI/CD-2088FF)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

---

## Overview

**Sentinel** is a self-healing cloud monitoring platform that demonstrates an end-to-end **Detect → Heal → Alert** workflow on AWS.

The system continuously monitors a Dockerized Flask application using **two independent detection mechanisms**:

* **Error-based detection** using structured logs and CloudWatch Metric Filters.
* **Heartbeat-based detection** for silent application failures where no logs are produced.

When a failure is detected, Sentinel automatically:

1. Detects the incident.
2. Invokes a Lambda function.
3. Uses AWS Systems Manager (SSM) Run Command to restart the Docker container.
4. Sends a human-readable notification via SNS.

The entire infrastructure is provisioned using **Terraform**, while deployments are fully automated through **GitHub Actions**.

---

# Architecture

```text
                   Git Push
                      │
                      ▼
              GitHub Actions CI/CD
                      │
                      ▼
                  Amazon ECR
                      │
                      ▼
          EC2 (Dockerized Flask App)
                      │
         ┌────────────┴────────────┐
         │                         │
         ▼                         ▼
Structured JSON Logs        Heartbeat Metric
         │                         │
         ▼                         ▼
 CloudWatch Logs         CloudWatch Metrics
         │                         │
         ▼                         ▼
 Metric Filter          Heartbeat Alarm
         │                         │
         └────────────┬────────────┘
                      ▼
             CloudWatch Alarm
                      │
                      ▼
          SNS (Machine-to-Machine)
                      │
                      ▼
             AWS Lambda Function
                      │
                      ▼
         AWS Systems Manager (SSM)
                      │
                      ▼
      docker restart sentinel-app
                      │
                      ▼
         SNS (Machine-to-Human)
                      │
                      ▼
                Email Notification
```

---

# Features

### Self-Healing Infrastructure

* Automatic application recovery without SSH
* AWS Systems Manager Run Command for secure remote execution
* Lambda-based remediation
* Human-readable incident notifications

---

### Dual Failure Detection

#### Error Detection

* Structured JSON logging
* CloudWatch Log Groups
* Metric Filters
* CloudWatch Alarms

Detects:

* Runtime exceptions
* Application errors
* Unexpected failures

---

#### Silent Failure Detection

Heartbeat metrics published every 60 seconds.

Detects:

* Container crashes
* Process termination
* Failures without logs

---

### CI/CD Pipeline

* GitHub Actions
* Docker image build
* Git SHA image tagging
* Amazon ECR deployment
* Zero-SSH deployments through SSM

---

### Infrastructure as Code

Entire infrastructure managed with Terraform.

Includes:

* EC2
* ECR
* IAM
* Lambda
* SNS
* CloudWatch
* Metric Filters
* Alarms

---

### Security

* Least-Privilege IAM
* No SSH access
* No hardcoded AWS credentials
* EC2 Instance Profile authentication
* Hand-written IAM policies

---

### Operations Dashboard

A built-in web dashboard provides:

* Alarm status
* Heartbeat monitor
* Incident timeline
* Failure simulation buttons
* Live system status

---

# Tech Stack

| Category         | Technologies                      |
| ---------------- | --------------------------------- |
| Language         | Python 3.12                       |
| Framework        | Flask                             |
| Containerization | Docker                            |
| Cloud            | AWS                               |
| Compute          | Amazon EC2                        |
| Registry         | Amazon ECR                        |
| Monitoring       | CloudWatch Logs, Metrics & Alarms |
| Serverless       | AWS Lambda                        |
| Messaging        | Amazon SNS                        |
| Remote Execution | AWS Systems Manager               |
| Infrastructure   | Terraform                         |
| CI/CD            | GitHub Actions                    |

---

# Project Structure

```text
Sentinel/
│
├── app.py
├── dashboard/
├── lambda/
│   └── remediation.py
├── infra/
│   ├── ec2.tf
│   ├── ecr.tf
│   ├── lambda.tf
│   ├── observability.tf
│   ├── provider.tf
│   └── sns.tf
├── .github/
│   └── workflows/
│       └── deploy.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

# Failure Recovery Workflow

## 1. Error-Based Failure

```text
Application Error
        │
        ▼
CloudWatch Logs
        │
        ▼
Metric Filter
        │
        ▼
CloudWatch Alarm
        │
        ▼
SNS
        │
        ▼
Lambda
        │
        ▼
SSM Run Command
        │
        ▼
docker restart sentinel-app
        │
        ▼
Email Notification
```

---

## 2. Silent Failure

```text
Application Crash
        │
        ▼
Heartbeat Stops
        │
        ▼
Heartbeat Alarm
        │
        ▼
SNS
        │
        ▼
Lambda
        │
        ▼
SSM Run Command
        │
        ▼
Container Restart
        │
        ▼
Email Notification
```

---

# Highlights

* End-to-end self-healing platform
* No SSH or Bastion Host required
* Detects both noisy and silent failures
* Fully automated deployments
* Infrastructure completely reproducible using Terraform
* Least-privilege IAM implementation
* Built-in operations dashboard
* Modular and production-inspired architecture

---

# Future Improvements

* OIDC authentication for GitHub Actions
* Terraform remote state (S3 + DynamoDB)
* Native CloudWatch Dashboard
* Lambda verification loop to confirm restart success
* Dead Letter Queue (DLQ) for Lambda failures
* Multi-instance Auto Scaling support

---

# Demo

### Trigger application errors

```bash
curl "http://<EC2-IP>:8000/simulate-failure?mode=error"
```

---

### Trigger silent crash

```bash
curl "http://<EC2-IP>:8000/simulate-failure?mode=crash"
```

---

### Health Check

```bash
curl http://<EC2-IP>:8000/health
```

---

### Dashboard

```text
http://<EC2-IP>:8000/dashboard
```

---

# Key Learnings

This project demonstrates practical experience with:

* Cloud Architecture
* AWS Monitoring & Observability
* Infrastructure as Code
* Serverless Computing
* CI/CD Pipelines
* Containerized Applications
* Cloud Security
* Automated Incident Response
* Production-style System Design

---
