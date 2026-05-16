#!/bin/bash

# Power Trading Backend Runner Script

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check if venv exists
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies if needed
echo -e "${YELLOW}Installing/updating dependencies...${NC}"
pip install -r requirements.txt

# Check if .env exists
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Creating .env file from template...${NC}"
    cp .env.example .env
    echo -e "${RED}Please update .env with your database credentials${NC}"
fi

# Run the application
echo -e "${GREEN}Starting Power Trading Backend...${NC}"
python -m app.main
