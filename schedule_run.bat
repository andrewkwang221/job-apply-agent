@echo off
cd /d C:\Users\Administrator\Downloads\MyGithub\job-apply-agent
call conda activate job-apply-agent
python run_pipeline.py full-run --email
