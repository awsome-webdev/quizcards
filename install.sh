#!/bin/bash

# --- Configuration ---
read -p "Do you want to install a systemctl service? (y/n): " answer

if [ "$answer" == "y" ]; then
read -p "Name of systemctl service: " APP_NAME
PROJECT_DIR=$(pwd)
VENV_DIR="$PROJECT_DIR"
USER_NAME=$(whoami)

echo "--- Starting setup for $APP_NAME ---"

# 1. Update and install python3-venv if not present
sudo apt update && sudo apt install -y python3-pip

# 3. Install Requirements
echo "Installing dependencies..."
sudo pip install -r "$PROJECT_DIR/requirements.txt" --break-system-packages

# 4. Create Systemd Service File
echo "Creating systemd service..."
sudo bash -c "cat > /etc/systemd/system/$APP_NAME.service <<EOF
[Unit]
Description=Gunicorn instance to serve $APP_NAME
After=network.target

[Service]
User=$USER_NAME
Group=www-data
WorkingDirectory=$PROJECT_DIR
ExecStart=python3 $PROJECT_DIR/app.py

[Install]
WantedBy=multi-user.target
EOF"

# 5. Start and Enable Service
echo "Starting service..."
sudo systemctl daemon-reload
sudo systemctl start $APP_NAME
sudo systemctl enable $APP_NAME

echo "--- Setup Complete ---"
echo "Status: $(sudo systemctl is-active $APP_NAME)"
else 
echo "exiting..."
exit
fi 
read -p "Want to configure the app now? (y/n): " answer
if [ "$answer" == "y" ]; then
    read -p "Port:" PORT
    read -p "Hackclub ai api key:" AI_KEY
    read -p "Hackclub search api key:" SEARCH_KEY
    read -p "Model to use for ai, it is recommended to use a fast one such as google/gemini-3-flash-preview:" MODEL
cat <<EOF > $PROJECT_DIR/keys.json
[
{
    "hcai": "$AI_KEY",
    "hcsearch": "$SEARCH_KEY",
    "model": "$MODEL",
    "port": "$PORT"
}
]
EOF
sudo systemctl restart $APP_NAME
echo "Configuration complete, service restarted."
fi