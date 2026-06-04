from pathlib import Path
p = Path("/opt/automation/email_summary.py")
s = p.read_text()
NL = chr(10)
bad = "reason = (it.get('reason','') or '').replace('" + NL + "','<br/>')"
good = "reason = (it.get('reason','') or '').replace(chr(10),'<br/>')"
print("bad found:", bad in s)
print("good already there:", good in s)
if bad in s:
    p.write_text(s.replace(bad, good))
    print("fixed")
