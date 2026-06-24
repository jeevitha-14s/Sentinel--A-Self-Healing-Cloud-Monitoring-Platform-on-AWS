terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "ap-south-1"
}

variable "incidents_topic_arn" {
  type        = string
  description = "ARN of the sentinel-incidents SNS topic (Feature 7). Leave empty until that topic exists."
  default     = ""
}

provider "aws" {
  region = var.aws_region
}
