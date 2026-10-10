#!/bin/bash

# This script connects to the Docker container and runs the Python script.

# Ensure the Docker container is running
if ! sudo docker ps | grep -q 'imports_container'; then
  echo "Docker container 'imports_container' is not running."
  exit 1
fi

# Execute the Python script inside the Docker container
sudo docker exec -it imports_container python3 /app/python/asset-management.py

