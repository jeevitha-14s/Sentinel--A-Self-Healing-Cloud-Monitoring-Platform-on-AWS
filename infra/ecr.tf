resource "aws_ecr_repository" "sentinel" {
  name                 = "sentinel"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "ecr_repo_url" {
  value = aws_ecr_repository.sentinel.repository_url
}
