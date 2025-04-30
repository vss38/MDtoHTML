import os
import argparse
import markdown
import json
from pathlib import Path
import html
import re
from collections import defaultdict
import base64 # For potential future image embedding

# --- Constants ---
DEFAULT_TITLE = "Generated Documentation"
SCRIPT_VERSION = "1.1.0"

# --- Helper Functions ---

def find_md_files(input_dir):
    """Recursively finds all Markdown files (.md) in the input directory."""
    md_files = []
    input_path = Path(input_dir).resolve()
    for root, _, files in os.walk(input_dir):
        root_path = Path(root).resolve()
        for file in files:
            if file.lower().endswith(".md"):
                full_path = root_path / file
                relative_path = full_path.relative_to(input_path)
                # Use normalized posix path as ID
                file_id = relative_path.as_posix()
                md_files.append({
                    "full_path": full_path,
                    "relative_path": relative_path,
                    "id": file_id,
                    "name": relative_path.stem  # Name without extension
                })
    return md_files

def build_file_tree(files_data, input_dir_path):
    """Builds a nested dictionary representing the file/folder hierarchy."""
    tree = {'type': 'folder', 'name': input_dir_path.name, 'path': '.', 'children': {}, 'id': '.'}

    for file_info in files_data:
        parts = list(file_info['relative_path'].parts)
        current_level = tree['children']
        current_path_parts = []

        for i, part in enumerate(parts):
            current_path_parts.append(part)
            item_path = Path(*current_path_parts).as_posix()

            if i == len(parts) - 1:  # It's the file
                if part not in current_level:
                    current_level[part] = {
                        'type': 'file',
                        'name': file_info['name'],
                        'path': file_info['relative_path'].as_posix(),
                        'id': file_info['id']
                    }
            else:  # It's a folder
                if part not in current_level:
                    current_level[part] = {
                        'type': 'folder',
                        'name': part,
                        'path': item_path,
                        'children': {},
                        'id': item_path # Folder path can serve as its ID for collapsing
                    }
                # Ensure it's marked as folder even if encountered later
                current_level[part]['type'] = 'folder'
                if 'children' not in current_level[part]:
                    current_level[part]['children'] = {}
                current_level = current_level[part]['children']
    return tree


def sort_tree_children(node):
    """Recursively sorts children of a node: folders first, then files, alphabetically."""
    if node.get('type') == 'folder' and 'children' in node:
        # Separate folders and files
        folders = []
        files = []
        for name, child in node['children'].items():
            sort_tree_children(child) # Sort deeper levels first
            if child['type'] == 'folder':
                folders.append((name, child))
            else:
                files.append((name, child))

        # Sort each list alphabetically by name (which is the key 'name')
        folders.sort(key=lambda item: item[1]['name'])
        files.sort(key=lambda item: item[1]['name'])

        # Combine sorted lists and update children
        sorted_children = {}
        for name, child in folders + files:
            sorted_children[name] = child
        node['children'] = sorted_children


def generate_sidebar_html(node, level=0):
    """Recursively generates the HTML for the sidebar navigation tree."""
    html_str = ""
    indent = "  " * (level + 1)

    # Use the original part name for sorting/display, use display name for rendering
    display_name = node.get('name', 'Unknown')
    item_id = node.get('id', '')

    if node.get('type') == 'folder':
        # Folder item
        html_str += f'{indent}<li class="folder{" closed" if level > 0 else ""}" data-folder-path="{html.escape(item_id)}">\n' # Start closed by default except root
        html_str += f'{indent}  <span class="toggle">▶</span>\n' # Arrow for toggle
        html_str += f'{indent}  <span class="folder-name">{html.escape(display_name)}</span>\n'
        if node.get('children'):
            html_str += f'{indent}  <ul{' style="display: none;"' if level > 0 else ""}>\n' # Hide children by default except root
            # Sort children before generating their HTML
            children_items = list(node['children'].values())
             # Separate folders and files for sorting
            child_folders = sorted([c for c in children_items if c['type'] == 'folder'], key=lambda x: x['name'])
            child_files = sorted([c for c in children_items if c['type'] == 'file'], key=lambda x: x['name'])

            for child in child_folders + child_files:
                 html_str += generate_sidebar_html(child, level + 1)
            html_str += f'{indent}  </ul>\n'
        html_str += f'{indent}</li>\n'

    elif node.get('type') == 'file':
        # File item - make it clickable to load content
        file_id = html.escape(node.get('id', ''))
        html_str += f'{indent}<li class="file">\n'
        # Use onclick for dynamic loading
        html_str += f'{indent}  <a href="#{file_id}" data-id="{file_id}" onclick="loadContent(\'{file_id}\'); return false;">{html.escape(display_name)}</a>\n'
        html_str += f'{indent}</li>\n'

    return html_str

def generate_initial_sidebar_html(tree):
    """Generates the root UL element for the sidebar."""
    root_ul = "<ul>\n"
    # Sort root children before generating
    children_items = list(tree.get('children', {}).values())
    child_folders = sorted([c for c in children_items if c['type'] == 'folder'], key=lambda x: x['name'])
    child_files = sorted([c for c in children_items if c['type'] == 'file'], key=lambda x: x['name'])

    for child in child_folders + child_files:
        root_ul += generate_sidebar_html(child, level=0)
    root_ul += "</ul>\n"
    return root_ul


def resolve_link(link_target, current_file_rel_path, all_files_map):
    """
    Resolves a Markdown link target (relative or absolute from root)
    to the unique file ID used in the HTML.
    Returns the file ID if found, otherwise None.
    """
    if not link_target.lower().endswith(".md"):
        return None # Not a link to another Markdown file

    current_dir = current_file_rel_path.parent
    target_path = None

    if link_target.startswith('/'):
        # Absolute path from the input root
        target_path = Path(link_target.lstrip('/'))
    elif link_target.startswith('../') or link_target.startswith('./') or '/' in link_target or '\\' in link_target:
         # Relative path from the current file's directory
        # Important: Resolve collapses '..' etc. Needs a base, use a dummy root for resolve()
        # Using os.path.normpath might be safer here if pathlib acts tricky
        # Let's try os.path.normpath on the joined path
        normalized_target = os.path.normpath(os.path.join(current_dir.as_posix(), link_target))
        target_path = Path(normalized_target)

        # Ensure it's still within the intended structure (prevent escaping root via ..)
        # Note: This basic check might not be foolproof for complex ../../.. scenarios
        if '..' in Path(normalized_target).as_posix().split('/'):
             try:
                 # Check if resolved path is still under root (represented by empty path)
                 target_path.relative_to('.')
             except ValueError:
                 print(f"Warning: Link '{link_target}' in '{current_file_rel_path}' potentially escapes root directory. Skipping conversion.")
                 return None

    else:
        # Simple filename relative to the current directory
        target_path = current_dir / link_target

    # Normalize to posix path for ID lookup
    target_id = target_path.as_posix()

    # Check if this resolved ID exists in our known files
    if target_id in all_files_map:
        return target_id
    else:
        # Try finding by base name across all files (less reliable, Obsidian-like fallback)
        target_basename = Path(link_target).stem
        found_files = [f_id for f_id, f_info in all_files_map.items() if f_info['name'] == target_basename]
        if len(found_files) == 1:
            print(f"Info: Ambiguous link '{link_target}' in '{current_file_rel_path}' resolved to '{found_files[0]}' by basename.")
            return found_files[0]
        elif len(found_files) > 1:
            print(f"Warning: Ambiguous link '{link_target}' in '{current_file_rel_path}' matches multiple files ({', '.join(found_files)}). Skipping conversion.")
            return None
        else:
            # Check if the target_path (normalized) exists directly
             if target_path.as_posix() in all_files_map:
                return target_path.as_posix()
             else:
                print(f"Warning: Link target '{link_target}' in '{current_file_rel_path}' resolved to '{target_id}' but file not found. Skipping conversion.")
                return None


def preprocess_markdown_links(md_content, current_file_rel_path, all_files_map):
    """
    Finds Markdown links and Obsidian-style [[WikiLinks]] (including aliases)
    to other .md files and converts them to onclick JS calls.
    """
    # --- Process standard Markdown links: [text](path/to/file.md) ---
    def replace_md_link(match):
        text = match.group(1)
        target = match.group(2)
        target_id = resolve_link(target, current_file_rel_path, all_files_map)
        if target_id:
            escaped_id = html.escape(target_id, quote=True)
            # Escape the link text itself to prevent XSS if it contains HTML
            escaped_text = html.escape(text)
            return f'<a href="#{escaped_id}" onclick="loadContent(\'{escaped_id}\'); return false;">{escaped_text}</a>'
        else:
            # Return the original link if resolution failed
            return match.group(0)

    # Regex for standard markdown links ending in .md (case-insensitive)
    md_link_pattern = r'\[([^\]]+)\]\(\s*([^)]+\.[mM][dD])\s*\)'
    md_content = re.sub(md_link_pattern, replace_md_link, md_content)

    # --- Process Obsidian WikiLinks: [[Link Name]], [[path/to/Link]], [[Link Name|Alias Text]] ---
    def replace_wiki_link(match):
        full_link_content = match.group(1).strip() # Get content within [[ ]] and strip whitespace

        target_specifier = full_link_content
        display_text = None

        # Check for alias separator '|'
        if '|' in full_link_content:
            parts = full_link_content.split('|', 1) # Split only on the first '|'
            target_specifier = parts[0].strip()
            display_text = parts[1].strip()
            # If display_text is empty after stripping, default back to target_specifier
            if not display_text:
                display_text = target_specifier
        else:
            # No alias, display text is the same as the target specifier
            display_text = target_specifier

        # --- Now resolve the target_specifier ---
        target_id = None
        # Check if the target_specifier looks like a path
        if '/' in target_specifier or '\\' in target_specifier:
            # Path is provided, treat like a relative markdown link
            # Add '.md' extension for resolve_link function
            target_md_path = target_specifier + ".md"
            target_id = resolve_link(target_md_path, current_file_rel_path, all_files_map)
        else:
            # Just a name, try to find a file with this stem anywhere
            target_basename = target_specifier # The part before '|' or the whole thing
            # Prioritize exact match in the same directory? (More complex, skip for now)
            # Simple global search by stem:
            found_files = [f_id for f_id, f_info in all_files_map.items() if f_info['name'] == target_basename]

            if len(found_files) == 1:
                target_id = found_files[0]
                # Optional: Add info log if display_text was used
                # if display_text != target_specifier:
                #    print(f"Info: WikiLink '[{full_link_content}]' in '{current_file_rel_path}' resolved to '{target_id}' by basename '{target_basename}'.")
            elif len(found_files) > 1:
                print(f"Warning: Ambiguous WikiLink target '{target_basename}' (from '[{full_link_content}]') in '{current_file_rel_path}' matches multiple files ({', '.join(found_files)}). Skipping conversion.")
                target_id = None
            else:
                 # Check if the non-path specifier directly corresponds to a root file ID (e.g., [[RootFile]])
                 potential_id = target_basename + ".md"
                 if potential_id in all_files_map:
                    target_id = potential_id
                 else:
                    print(f"Warning: WikiLink target '{target_basename}' (from '[{full_link_content}]') in '{current_file_rel_path}' not found. Skipping conversion.")
                    target_id = None

        # --- Generate the HTML ---
        if target_id:
            escaped_id = html.escape(target_id, quote=True)
            # IMPORTANT: Escape the display_text to prevent rendering raw HTML if the alias contains it
            escaped_display_text = html.escape(display_text)
            return f'<a href="#{escaped_id}" class="internal-link" onclick="loadContent(\'{escaped_id}\'); return false;">{escaped_display_text}</a>'
        else:
            # Return the original wikilink text, escaped, marked as unresolved
            # Use the full original content inside [[ ]] for clarity on what failed
            return f'<span class="unresolved-link">[[{html.escape(full_link_content)}]]</span>'

    # Regex for WikiLinks - captures everything inside [[ ]]
    wikilink_pattern = r'\[\[([^\]]+)\]\]'
    md_content = re.sub(wikilink_pattern, replace_wiki_link, md_content)

    return md_content


def generate_html(title, sidebar_html, content_data, root_doc_id, css, js):
    """Generates the final single HTML file content."""
    # Escape backticks and ${} in JS code if embedding directly in f-string
    # A safer way is to keep JS in its own block or use placeholders
    js_escaped = js.replace('`', '\\`').replace('${', '\\${')

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(title)}</title>
    <style>
        {css}
    </style>
</head>
<body>
    <button id="sidebar-toggle" aria-label="Toggle Navigation">☰</button>

    <nav id="sidebar">
        <div class="sidebar-content">
            {sidebar_html}
        </div>
    </nav>

    <main id="content-area">
        <!-- Content will be loaded here -->
        <p>Loading...</p>
    </main>

    <script>
        // Embed content data safely using JSON
        const contentData = {json.dumps(content_data, indent=2)};
        const rootDocId = {json.dumps(root_doc_id)};

        // Embed JavaScript logic
        {js}
    </script>
</body>
</html>"""
    return html_template

# --- CSS Styles (Obsidian Dark Theme Inspired) ---
CSS_STYLES = """
:root {
  --bg-color: #202020;
  --text-color: #dcdcdc;
  --sidebar-bg: #2a2a2a;
  --sidebar-border: #444;
  --content-bg: #282828; /* Slightly different for contrast */
  --link-color: #8ab4f8;
  --link-hover-color: #a1c5ff;
  --accent-color: #8a7fdb; /* Purplish accent */
  --heading-color: #ededed;
  --border-color: #4a4a4a;
  --code-bg: #333333;
  --code-text: #f0f0f0;
  --pre-bg: #2d2d2d;
  --blockquote-border: #555;
  --blockquote-text: #b0b0b0;
  --table-border: #555;
  --table-header-bg: #383838;
  --sidebar-width: 280px;
  --transition-speed: 0.3s;
}

* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

html, body {
  height: 100%;
  overflow: hidden; /* Prevent body scroll when sidebar is open */
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen-Sans, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
  background-color: var(--bg-color);
  color: var(--text-color);
  line-height: 1.6;
}

/* Dimming overlay for when sidebar is open */
body::after {
    content: '';
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5); /* Dimming effect */
    z-index: 999; /* Below sidebar (1000), above content */
    opacity: 0;
    visibility: hidden;
    transition: opacity var(--transition-speed) ease, visibility var(--transition-speed) ease;
    pointer-events: none; /* Allow clicks through when hidden */
}
body.sidebar-open::after {
    opacity: 1;
    visibility: visible;
    pointer-events: auto; /* Intercept clicks when visible, handled by JS */
}



#sidebar-toggle {
  position: fixed;
  top: 10px;
  left: 10px;
  z-index: 1001;
  background: var(--sidebar-bg);
  color: var(--text-color);
  border: 1px solid var(--sidebar-border);
  border-radius: 4px;
  padding: 8px 12px;
  font-size: 1.2em;
  cursor: pointer;
  transition: background-color 0.2s;
}

#sidebar-toggle:hover {
    background: var(--accent-color);
    color: white;
}

#sidebar {
  width: var(--sidebar-width);
  height: 100%;
  position: fixed;
  top: 0;
  left: calc(-1 * var(--sidebar-width)); /* Start hidden */
  background-color: var(--sidebar-bg);
  border-right: 1px solid var(--sidebar-border);
  z-index: 1000;
  overflow-y: auto;
  overflow-x: hidden;
  transition: left var(--transition-speed) ease;
  padding-top: 50px; /* Space for toggle button */
}

body.sidebar-open #sidebar {
    left: 0;
}

.sidebar-content {
    padding: 15px;
}

#sidebar ul {
  list-style: none;
  padding-left: 15px; /* Indentation for levels */
}

#sidebar li {
  margin-bottom: 5px;
  word-wrap: break-word; /* Prevent long names from overflowing */
  white-space: normal; /* Allow wrapping */
}

#sidebar li.folder {
    cursor: pointer;
    user-select: none; /* Prevent text selection on toggle click */
}
#sidebar li.folder > .folder-name {
    font-weight: bold;
}

#sidebar li.folder > .toggle {
    display: inline-block;
    width: 1em;
    margin-right: 5px;
    text-align: center;
    transition: transform 0.2s ease;
}
#sidebar li.folder.closed > .toggle {
    transform: rotate(-90deg);
}
#sidebar li.folder.open > .toggle {
     transform: rotate(0deg); /* Pointing down */
}


#sidebar a {
  color: var(--text-color);
  text-decoration: none;
  display: block;
  padding: 3px 0;
  transition: color 0.2s ease;
}

#sidebar a:hover {
  color: var(--link-hover-color);
}
#sidebar a.active {
    color: var(--accent-color);
    font-weight: bold;
}

/* Styles for unresolved links */
.unresolved-link {
    color: #ff7b7b; /* Reddish color */
    font-style: italic;
    cursor: not-allowed;
}

/* Styles for internal links */
.internal-link {
    color: var(--link-color);
    text-decoration: underline;
    text-decoration-style: solid;
    cursor: pointer;
}
.internal-link:hover {
    color: var(--link-hover-color);
    text-decoration: underline;
}


#content-area {
  width: 100%;
  padding: 25px 40px;
  padding-bottom: 5rem;
  background-color: var(--content-bg);
  overflow-y: auto;
  height: 100vh; /* Ensure it takes full viewport height */
  margin-left: 0; /* Adjusted by JS potentially */
}

/* Markdown Content Styling */
#content-area h1, #content-area h2, #content-area h3, #content-area h4, #content-area h5, #content-area h6 {
  color: var(--heading-color);
  margin-top: 1.5em;
  margin-bottom: 0.8em;
  font-weight: 600;
  border-bottom: 1px solid var(--border-color);
  padding-bottom: 0.3em;
}
#content-area h1 { font-size: 2em; }
#content-area h2 { font-size: 1.7em; }
#content-area h3 { font-size: 1.4em; }
#content-area h4 { font-size: 1.2em; }
#content-area h5 { font-size: 1.1em; }
#content-area h6 { font-size: 1em; color: var(--accent-color); }

#content-area p {
  margin-bottom: 1em;
}

#content-area a {
  color: var(--link-color);
  text-decoration: none;
}
#content-area a:hover {
  color: var(--link-hover-color);
  text-decoration: underline;
}
/* Ensure internal links processed by python get the right color */
#content-area a[onclick^="loadContent"] {
    color: var(--link-color);
    text-decoration: underline;
    text-decoration-style: solid;
    cursor: pointer;
}
#content-area a[onclick^="loadContent"]:hover {
    color: var(--link-hover-color);
    text-decoration: underline;
}


#content-area ul, #content-area ol {
  margin-left: 2em;
  margin-bottom: 1em;
}
#content-area li {
    margin-bottom: 0.4em;
}
/* Task Lists */
#content-area ul li.task-list-item {
    list-style-type: none;
    margin-left: -1.5em; /* Adjust negative margin if needed */
}
#content-area input[type="checkbox"] {
    margin-right: 0.5em;
    vertical-align: middle; /* Align checkbox nicely */
}


#content-area blockquote {
  border-left: 4px solid var(--blockquote-border);
  margin: 1.5em 0;
  padding: 0.5em 1em;
  color: var(--blockquote-text);
  background-color: rgba(255, 255, 255, 0.03); /* Subtle background */
}

#content-area code {
  background-color: var(--code-bg);
  color: var(--code-text);
  padding: 0.2em 0.4em;
  border-radius: 3px;
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, Courier, monospace;
  font-size: 0.9em;
}

#content-area pre {
  background-color: var(--pre-bg);
  border: 1px solid var(--border-color);
  padding: 1em;
  margin-bottom: 1.5em;
  overflow-x: auto;
  border-radius: 4px;
}

#content-area pre code {
  background-color: transparent; /* Code inside pre shouldn't have its own background */
  padding: 0;
  font-size: 0.9em;
  line-height: 1.4;
}

/* Pygments Highlighting */
.codehilite { background: var(--pre-bg); } /* Ensure background consistency */
.codehilite .hll { background-color: #404040 } /* Highlighted line */
.codehilite .c { color: #999999; font-style: italic } /* Comment */
.codehilite .err { color: #ff7b7b; background-color: #4d2a2a } /* Error */
.codehilite .k { color: #f0c674 } /* Keyword */
.codehilite .l { color: #cc99cc } /* Literal */
.codehilite .n { color: var(--text-color) } /* Name */
.codehilite .o { color: #ededed } /* Operator */
.codehilite .p { color: #ededed } /* Punctuation */
.codehilite .cm { color: #999999; font-style: italic } /* Comment.Multiline */
.codehilite .cp { color: #cc99cc } /* Comment.Preproc */
.codehilite .c1 { color: #999999; font-style: italic } /* Comment.Single */
.codehilite .cs { color: #999999; font-weight: bold; font-style: italic } /* Comment.Special */
.codehilite .gd { color: #d54e53 } /* Generic.Deleted */
.codehilite .ge { font-style: italic } /* Generic.Emph */
.codehilite .gr { color: #ff0000 } /* Generic.Error */
.codehilite .gh { color: #ffffff; font-weight: bold } /* Generic.Heading */
.codehilite .gi { color: #b9ca4a } /* Generic.Inserted */
.codehilite .go { color: #404040 } /* Generic.Output */
.codehilite .gp { color: #aaaaaa } /* Generic.Prompt */
.codehilite .gs { font-weight: bold } /* Generic.Strong */
.codehilite .gu { color: #ffffff; text-decoration: underline } /* Generic.Subheading */
.codehilite .gt { color: #ff0000 } /* Generic.Traceback */
.codehilite .kc { color: #f0c674 } /* Keyword.Constant */
.codehilite .kd { color: #f0c674 } /* Keyword.Declaration */
.codehilite .kn { color: #f0c674 } /* Keyword.Namespace */
.codehilite .kp { color: #f0c674 } /* Keyword.Pseudo */
.codehilite .kr { color: #f0c674 } /* Keyword.Reserved */
.codehilite .kt { color: #e7c547 } /* Keyword.Type */
.codehilite .ld { color: #cc99cc } /* Literal.Date */
.codehilite .m { color: #cc99cc } /* Literal.Number */
.codehilite .s { color: #b9ca4a } /* Literal.String */
.codehilite .na { color: #b294bb } /* Name.Attribute */
.codehilite .nb { color: var(--text-color) } /* Name.Builtin */
.codehilite .nc { color: #e7c547 } /* Name.Class */
.codehilite .no { color: #cc99cc } /* Name.Constant */
.codehilite .nd { color: #ededed } /* Name.Decorator */
.codehilite .ni { color: #ededed } /* Name.Entity */
.codehilite .ne { color: #d54e53; font-weight: bold } /* Name.Exception */
.codehilite .nf { color: #b294bb } /* Name.Function */
.codehilite .nl { color: #ededed } /* Name.Label */
.codehilite .nn { color: #e7c547 } /* Name.Namespace */
.codehilite .nx { color: #b294bb } /* Name.Other */
.codehilite .py { color: #ededed } /* Name.Property */
.codehilite .nt { color: #f0c674 } /* Name.Tag */
.codehilite .nv { color: #d54e53 } /* Name.Variable */
.codehilite .ow { color: #f0c674 } /* Operator.Word */
.codehilite .w { color: #ededed } /* Text.Whitespace */
.codehilite .mf { color: #cc99cc } /* Literal.Number.Float */
.codehilite .mh { color: #cc99cc } /* Literal.Number.Hex */
.codehilite .mi { color: #cc99cc } /* Literal.Number.Integer */
.codehilite .mo { color: #cc99cc } /* Literal.Number.Oct */
.codehilite .sb { color: #b9ca4a } /* Literal.String.Backtick */
.codehilite .sc { color: #b9ca4a } /* Literal.String.Char */
.codehilite .sd { color: #999999 } /* Literal.String.Doc */
.codehilite .s2 { color: #b9ca4a } /* Literal.String.Double */
.codehilite .se { color: #cc99cc } /* Literal.String.Escape */
.codehilite .sh { color: #b9ca4a } /* Literal.String.Heredoc */
.codehilite .si { color: #b9ca4a } /* Literal.String.Interpol */
.codehilite .sx { color: #b9ca4a } /* Literal.String.Other */
.codehilite .sr { color: #b9ca4a } /* Literal.String.Regex */
.codehilite .s1 { color: #b9ca4a } /* Literal.String.Single */
.codehilite .ss { color: #b9ca4a } /* Literal.String.Symbol */
.codehilite .bp { color: var(--text-color) } /* Name.Builtin.Pseudo */
.codehilite .vc { color: #d54e53 } /* Name.Variable.Class */
.codehilite .vg { color: #d54e53 } /* Name.Variable.Global */
.codehilite .vi { color: #d54e53 } /* Name.Variable.Instance */
.codehilite .il { color: #cc99cc } /* Literal.Number.Integer.Long */

#content-area table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 1.5em;
  border: 1px solid var(--table-border);
}
#content-area th, #content-area td {
  border: 1px solid var(--table-border);
  padding: 0.7em 1em;
  text-align: left;
}
#content-area th {
  background-color: var(--table-header-bg);
  font-weight: bold;
}
#content-area tr:nth-child(even) {
  background-color: rgba(255, 255, 255, 0.03); /* Subtle striping */
}

#content-area hr {
    border: none;
    border-top: 2px solid var(--border-color);
    margin: 2em 0;
}

/* Ensure content area scrolls independently */
@media (min-width: 769px) { /* Apply only when sidebar can potentially overlap */
    body.sidebar-open #content-area {
        /* Optional: prevent content shift */
        /* margin-left: var(--sidebar-width); */
    }
}
"""

# --- JavaScript Logic ---
JS_CODE = """
document.addEventListener('DOMContentLoaded', function() {
    const sidebar = document.getElementById('sidebar');
    const contentArea = document.getElementById('content-area');
    const toggleButton = document.getElementById('sidebar-toggle');
    const body = document.body;

    // --- Sidebar Toggle ---
    toggleButton.addEventListener('click', () => {
        body.classList.toggle('sidebar-open');
    });

    // --- Close sidebar on clicking outside (on the overlay/content area) ---
    document.addEventListener('click', function(event) {
        // Check if the sidebar is open
        // Check if the click target is NOT the sidebar or a descendant of the sidebar
        // Check if the click target is NOT the toggle button
        if (body.classList.contains('sidebar-open') &&
            !sidebar.contains(event.target) &&
            !toggleButton.contains(event.target))
        {
            body.classList.remove('sidebar-open');
        }
    });

    // --- Content Loading ---
    window.loadContent = function(fileId) {
        console.log("Loading content for:", fileId);
        const contentDiv = document.getElementById('content-area');
        if (contentData[fileId] !== undefined) {
            contentDiv.innerHTML = contentData[fileId];
            updateActiveLink(fileId);
            contentDiv.scrollTop = 0; // Scroll to top
            window.location.hash = fileId; // Update hash

            // Close sidebar after navigation, especially useful on mobile
            // Don't need the width check anymore, consistent behavior is fine
             body.classList.remove('sidebar-open');

        } else {
            contentDiv.innerHTML = `<p>Error: Content for '${fileId}' not found.</p>`;
            console.error("Content not found for ID:", fileId);
        }
    }

    // --- Sidebar Active Link Highlighting ---
    function updateActiveLink(activeId) {
        const links = sidebar.querySelectorAll('.sidebar-content a');
        links.forEach(link => {
            if (link.dataset.id === activeId) {
                link.classList.add('active');
                // Optional: Expand parent folders if needed
                let parentLi = link.closest('li.folder');
                 while(parentLi) {
                     if (parentLi.classList.contains('closed')) {
                         toggleFolder(parentLi, true); // Force open
                     }
                     parentLi = parentLi.parentElement.closest('li.folder');
                 }

            } else {
                link.classList.remove('active');
            }
        });
    }

    // --- Sidebar Folder Toggling ---
    function toggleFolder(folderLi, forceOpen = false) {
         const childUl = folderLi.querySelector(':scope > ul');
         const toggle = folderLi.querySelector(':scope > span.toggle');

         if (childUl && toggle) {
             if (forceOpen) {
                 if (folderLi.classList.contains('closed')) { // Only act if currently closed
                    folderLi.classList.remove('closed');
                    folderLi.classList.add('open');
                    childUl.style.display = 'block';
                    toggle.textContent = '▼';
                 }
             } else {
                 folderLi.classList.toggle('closed');
                 folderLi.classList.toggle('open');
                 if (folderLi.classList.contains('open')) {
                     childUl.style.display = 'block';
                     toggle.textContent = '▼';
                 } else {
                     childUl.style.display = 'none';
                     toggle.textContent = '▶';
                 }
             }
         }
    }

    // Add click listeners to folder spans/toggles
    sidebar.addEventListener('click', function(event) {
        const target = event.target;
        if (target.classList.contains('toggle') || target.classList.contains('folder-name')) {
             const folderLi = target.closest('li.folder');
             if (folderLi) {
                 toggleFolder(folderLi);
             }
        }
        // Prevent clicks inside sidebar from triggering the document click listener
        // event.stopPropagation(); // Be careful with stopPropagation, might interfere later. Usually check target is enough.
    });


    // --- Initial Load ---
    let initialFileId = rootDocId;
    // Check if there's a hash in the URL corresponding to a file ID
    if (window.location.hash) {
        const hashId = window.location.hash.substring(1); // Remove '#'
        if (contentData[hashId] !== undefined) {
            initialFileId = hashId;
            console.log("Loading content from URL hash:", initialFileId);
        } else {
             console.warn(`Hash ID '${hashId}' not found, loading root document.`);
        }
    }

    if (initialFileId && contentData[initialFileId] !== undefined) {
        loadContent(initialFileId);
    } else if (rootDocId && contentData[rootDocId] !== undefined) {
        // Fallback to rootDocId if hash was invalid but rootDocId exists
        console.log("Hash invalid or points to non-existent content, loading specified root:", rootDocId);
        loadContent(rootDocId);
    }
     else {
        const firstAvailableId = Object.keys(contentData)[0];
        if(firstAvailableId){
             console.warn("No initial document specified or found, loading first available:", firstAvailableId);
             loadContent(firstAvailableId);
        } else {
             document.getElementById('content-area').innerHTML = "<p>No content available to display.</p>";
             console.warn("No content data found at all.");
        }
    }

     // Initialize folder states (arrows)
     const allFolderLis = sidebar.querySelectorAll('li.folder');
     allFolderLis.forEach(li => {
         const toggle = li.querySelector(':scope > span.toggle');
         if (toggle) {
             if (li.classList.contains('closed')) {
                 toggle.textContent = '▶';
             } else {
                 // Ensure initially open folders (root) also have the correct arrow
                 toggle.textContent = '▼';
                 const childUl = li.querySelector(':scope > ul');
                 if(childUl) childUl.style.display = 'block'; // Ensure visible if not closed
             }
         }
     });

});
"""

# --- Main Execution ---

def main():
    parser = argparse.ArgumentParser(
        description=f"Convert a directory of Markdown files into a single self-contained HTML documentation file (v{SCRIPT_VERSION}).",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-i", "--input-dir",
        required=True,
        help="Path to the root directory containing Markdown files."
    )
    parser.add_argument(
        "-o", "--output-file",
        required=True,
        help="Path for the generated HTML output file."
    )
    parser.add_argument(
        "-r", "--root-doc",
        default=None,
        help="Relative path (from input-dir) to the Markdown file to display initially (e.g., 'README.md' or 'docs/index.md'). Defaults to the first found README.md or the first file alphabetically."
    )
    parser.add_argument(
        "-t", "--title",
        default=None,
        help="Title for the HTML document. Defaults to the output filename or input directory name."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {SCRIPT_VERSION}"
    )

    args = parser.parse_args()

    input_path = Path(args.input_dir)
    output_path = Path(args.output_file)

    if not input_path.is_dir():
        print(f"Error: Input directory not found: {args.input_dir}")
        return

    print(f"Scanning '{input_path}' for Markdown files...")
    md_files_data = find_md_files(args.input_dir)

    if not md_files_data:
        print("Warning: No Markdown files found.")
        # Create an empty HTML file? Or just exit? Let's create a minimal one.
        # We'll handle this further down.

    # Create a map for quick lookup by ID (normalized relative path)
    files_map = {file_info['id']: file_info for file_info in md_files_data}
    print(f"Found {len(md_files_data)} Markdown file(s).")

    # Determine the root document ID
    root_doc_id = None
    if args.root_doc:
        requested_root_id = Path(args.root_doc).as_posix() # Normalize path separators
        if requested_root_id in files_map:
            root_doc_id = requested_root_id
        else:
            print(f"Warning: Specified root document '{args.root_doc}' (resolved to ID '{requested_root_id}') not found. Trying defaults...")

    if not root_doc_id:
        # Try finding README.md or readme.md at the root level
        readme_files = [f_id for f_id in files_map if Path(f_id).name.lower() == "readme.md" and len(Path(f_id).parts) == 1]
        if readme_files:
             readme_files.sort() # Ensure consistent choice if multiple casings exist
             root_doc_id = readme_files[0]
             print(f"Using root README: '{root_doc_id}'")
        elif md_files_data:
            # Fallback to the first file alphabetically by full path ID
             md_files_data.sort(key=lambda x: x['id'])
             root_doc_id = md_files_data[0]['id']
             print(f"Using first file alphabetically as root: '{root_doc_id}'")
        else:
             print("Warning: No Markdown files found, cannot determine root document.")
             root_doc_id = None # Explicitly set to None


    print("Processing Markdown files and converting to HTML...")
    html_contents = {}
    md_extensions = [
        'extra',  # Includes Abbreviations, Attribute Lists, Definition Lists, Fenced Code Blocks, Footnotes, Tables, SmartyPants
        'meta',   # Handles front-matter metadata (though we don't explicitly use it here)
        'sane_lists', # More predictable list behavior
        'codehilite', # Requires Pygments. Adds CSS classes for syntax highlighting
        'toc', # Table of contents generation (optional, can be useful)
        'nl2br', # Newlines become <br> tags (GFM style)
        'markdown_checklist.extension', # For task lists like - [ ] or - [x]
    ]
    # Configuration for codehilite (optional, can customize CSS class, etc.)
    md_extension_configs = {
        'codehilite': {
            'css_class': 'codehilite', # Default class name
            'linenums': False,          # Don't add line numbers by default
            'guess_lang': True,        # Try to guess language if not specified
        }
    }

    # Initialize Markdown parser once
    md_parser = markdown.Markdown(extensions=md_extensions, extension_configs=md_extension_configs)

    for file_info in md_files_data:
        try:
            raw_content = file_info['full_path'].read_text(encoding='utf-8')
            # Preprocess links *before* parsing Markdown
            preprocessed_content = preprocess_markdown_links(
                raw_content,
                file_info['relative_path'],
                files_map
            )
            # Convert Markdown to HTML
            html_body = md_parser.convert(preprocessed_content)
            html_contents[file_info['id']] = html_body
            md_parser.reset() # Reset parser state for the next file
        except Exception as e:
            print(f"Error processing file {file_info['full_path']}: {e}")
            html_contents[file_info['id']] = f"<p>Error processing this file: {e}</p>"


    print("Building file tree for navigation...")
    file_tree = build_file_tree(md_files_data, input_path)
    # sort_tree_children(file_tree) # Sorting now happens within generate_sidebar_html

    print("Generating sidebar HTML...")
    sidebar_html = generate_initial_sidebar_html(file_tree)

    # Determine HTML title
    page_title = args.title if args.title else output_path.stem.replace('_', ' ').replace('-', ' ').title()
    if not args.title and not output_path.stem: # Handle edge case where output has no stem (e.g., ".")
        page_title = input_path.name.title()

    print("Generating final HTML file...")
    final_html = generate_html(
        title=page_title,
        sidebar_html=sidebar_html,
        content_data=html_contents,
        root_doc_id=root_doc_id,
        css=CSS_STYLES,
        js=JS_CODE
    )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True) # Ensure output directory exists
        output_path.write_text(final_html, encoding='utf-8')
        print(f"Successfully generated HTML file: {output_path.resolve()}")
    except Exception as e:
        print(f"Error writing output file {output_path}: {e}")

if __name__ == "__main__":
    main()