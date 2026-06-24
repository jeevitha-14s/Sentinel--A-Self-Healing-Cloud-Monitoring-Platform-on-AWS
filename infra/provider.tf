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
  type        = string
  description = "AWS region to deploy Sentinel resources into."
  default     = "ap-south-1"
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
