with open('dashboard/src/index.css', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, l in enumerate(lines):
    if '@media' in l or 'widget-grid' in l or 'widget-split' in l:
        print(f'{i+1}: {l}', end='')
