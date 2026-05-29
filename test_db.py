import database
from main import run_scraper_single_bg

# Create a clean DB just in case
database.init_db()

# Run the single bg function (which I modified to instantiate its own db)
print("Running run_scraper_single_bg...")
run_scraper_single_bg("oficina mecanica em Presidente Prudente", 2)
print("Finished. Checking DB...")

db = database.SessionLocal()
leads = db.query(database.Lead).all()
print(f"Total leads in DB: {len(leads)}")
for lead in leads:
    print(f"- {lead.name} | {lead.phone} | {lead.address}")
db.close()
