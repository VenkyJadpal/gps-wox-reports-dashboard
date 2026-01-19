# 🚀 Deploy GPS Report Dashboard to Render

This guide will walk you through deploying your Flask application to Render's free tier.

## Prerequisites

- [ ] GitHub account
- [ ] Render account (sign up at https://render.com - free)
- [ ] Your code pushed to a GitHub repository

## Step 1: Prepare Your Repository

### 1.1 Verify Git Configuration

Check that sensitive files are excluded from Git:

```bash
# Verify .gitignore includes these patterns
cat .gitignore | grep -E "(\.env|\.pem)"
```

The `.gitignore` should already exclude `.env` and `*.pem` files.

### 1.2 Commit and Push Deployment Files

```bash
# Add the new deployment files
git add render.yaml .renderignore requirements.txt app.py

# Commit changes
git commit -m "Add Render deployment configuration"

# Push to GitHub
git push origin main
```

> **Note**: Replace `main` with your branch name if different (e.g., `master`)

## Step 2: Create Render Web Service

### 2.1 Sign Up / Log In to Render

1. Go to https://render.com
2. Sign up with GitHub (recommended for easier integration)

### 2.2 Create New Web Service

1. Click **"New +"** → **"Web Service"**
2. Connect your GitHub repository:
   - Click **"Connect account"** if first time
   - Select your repository: `gps-opx`
3. Configure the service:
   - **Name**: `gps-report-dashboard` (or your preferred name)
   - **Region**: Choose closest to you (e.g., Oregon)
   - **Branch**: `main` (or your default branch)
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Instance Type**: `Free`

## Step 3: Configure Environment Variables

In the Render dashboard, scroll to **"Environment Variables"** section and add:

| Key | Value |
|-----|-------|
| `SSH_HOST` | `devops@34.166.43.194` |
| `DB_NAME` | `gpswox_web` |
| `DB_USER` | `root` |
| `DB_PASSWORD` | *(your database password)* |
| `DB_HOST` | `127.0.0.1` |
| `DB_PORT` | `3306` |
| `SSH_KEY` | `gpswox-ssh-key.pem` |

> **Important**: Keep `DB_PASSWORD` empty if there's no password, otherwise enter the actual password.

## Step 4: Upload SSH Private Key

### Option A: Using Secret Files (Recommended)

1. In Render dashboard, go to **"Environment"** tab
2. Scroll to **"Secret Files"**
3. Click **"Add Secret File"**
4. Set:
   - **Filename**: `gpswox-ssh-key.pem`
   - **Contents**: Copy and paste the entire contents of your local `gpswox-ssh-key.pem` file
5. Click **"Save"**

### Option B: Using Environment Variable

Alternatively, you can encode the key as base64 and store it as an environment variable, but Option A is simpler.

## Step 5: Deploy

1. Click **"Create Web Service"** at the bottom
2. Render will automatically:
   - Clone your repository
   - Install dependencies
   - Start your application
3. Wait 2-3 minutes for the build to complete

## Step 6: Access Your Application

1. Once deployed, Render provides a URL like: `https://gps-report-dashboard.onrender.com`
2. Click the URL to open your application
3. You should see the login page
4. Log in with:
   - **Email**: `admin@wakecap.com`
   - **Password**: `wakecap@2026!`

## Step 7: Test the Application

1. Log in to the dashboard
2. Select a project
3. Choose a report type
4. Select date range
5. Click "Generate Report"
6. Verify the report downloads successfully

## Important Notes

### ⚠️ Free Tier Limitations

- **Spin Down**: Your app will spin down after 15 minutes of inactivity
- **Wake Up Time**: First request after inactivity takes ~30-50 seconds
- **Monthly Hours**: 750 hours/month (enough for 24/7 operation)

### 🔒 Security Reminders

- Never commit `.env` or `.pem` files to Git
- Use Render's environment variables for all secrets
- Rotate SSH keys periodically

### 🐛 Troubleshooting

#### Application Won't Start
- Check Render logs: Dashboard → "Logs" tab
- Verify all environment variables are set correctly
- Ensure SSH key file is uploaded

#### Database Connection Fails
- Verify SSH_HOST is correct
- Check SSH key has proper permissions on remote server
- Confirm database credentials are correct

#### Reports Not Generating
- Check application logs for SQL errors
- Verify user email exists in database
- Test SSH connection manually

### 📊 Monitoring

- **Logs**: View real-time logs in Render dashboard
- **Metrics**: Check CPU, memory usage in "Metrics" tab
- **Alerts**: Set up email notifications for deployment failures

## Updating Your Application

When you push changes to GitHub:

1. Render automatically detects the push
2. Triggers a new build
3. Deploys the updated version
4. Zero-downtime deployment

To disable auto-deploy:
- Go to Settings → "Build & Deploy"
- Toggle "Auto-Deploy" off

## Custom Domain (Optional)

To use your own domain:

1. Go to Settings → "Custom Domain"
2. Add your domain
3. Update DNS records as instructed
4. Wait for SSL certificate provisioning

## Need Help?

- **Render Docs**: https://render.com/docs
- **Render Community**: https://community.render.com
- **Application Logs**: Check Render dashboard for detailed error messages

---

**Your application is now deployed! 🎉**
