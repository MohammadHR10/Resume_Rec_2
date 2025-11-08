import re

# Read the current app.py file
with open('app.py', 'r') as f:
    content = f.read()

# Find the start of tab1 and tab2
lines = content.split('\n')
new_lines = []
inside_tab1 = False
inside_tab2 = False

for i, line in enumerate(lines):
    if 'with tab1:' in line:
        inside_tab1 = True
        inside_tab2 = False
        new_lines.append(line)
    elif 'with tab2:' in line:
        inside_tab1 = False
        inside_tab2 = True
        new_lines.append(line)
    elif inside_tab1 and line.strip() and not line.startswith('    ') and not line.startswith('with '):
        # Add indentation for tab1 content
        if line.startswith('st.') or line.startswith('if ') or line.startswith('upload_') or line.startswith('job_') or line.startswith('department') or line.startswith('#'):
            new_lines.append('    ' + line)
        else:
            new_lines.append(line)
    else:
        new_lines.append(line)

# Write the corrected content
with open('app_fixed.py', 'w') as f:
    f.write('\n'.join(new_lines))

print("Fixed indentation saved to app_fixed.py")