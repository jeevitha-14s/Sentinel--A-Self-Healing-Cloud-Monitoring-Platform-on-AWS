# ── Data sources ────────────────────────────────────────────────────────────

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "map-public-ip-on-launch"
    values = ["true"]
  }
}

# ── IAM — instance profile ───────────────────────────────────────────────────

resource "aws_iam_role" "sentinel_ec2" {
  name = "sentinel-ec2"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.sentinel_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ecr_pull" {
  name = "ecr-pull"
  role = aws_iam_role.sentinel_ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.sentinel.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "sentinel_ec2" {
  name = "sentinel-ec2"
  role = aws_iam_role.sentinel_ec2.name
}

# ── Security group ───────────────────────────────────────────────────────────

resource "aws_security_group" "sentinel" {
  name   = "sentinel"
  vpc_id = data.aws_vpc.default.id

  ingress {
    description = "app"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── EC2 instance ─────────────────────────────────────────────────────────────

resource "aws_instance" "sentinel" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.micro"
  subnet_id              = data.aws_subnets.public.ids[0]
  vpc_security_group_ids = [aws_security_group.sentinel.id]
  iam_instance_profile   = aws_iam_instance_profile.sentinel_ec2.name

  user_data_replace_on_change = true

  user_data = <<-EOF
    #!/bin/bash
    dnf install -y docker amazon-ssm-agent
    systemctl enable --now docker
    systemctl enable --now amazon-ssm-agent
  EOF

  tags = {
    Name = "sentinel"
  }
}

output "instance_id" {
  value = aws_instance.sentinel.id
}

output "public_ip" {
  value = aws_instance.sentinel.public_ip
}
