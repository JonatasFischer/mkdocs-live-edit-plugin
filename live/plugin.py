"""mkdocs-live-edit-plugin

An MkDocs plugin that enables live editing of wiki pages directly from the browser.
"""

import asyncio
import json
import os
import string
import threading
import time
from logging import Logger, getLogger
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import websockets.client
import websockets.server
from mkdocs.config import config_options
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.livereload import LiveReloadServer, _timestamp
from mkdocs.plugins import BasePlugin
from mkdocs.structure.files import File, Files
from mkdocs.structure.pages import Page
from websockets import serve


class LiveEditPlugin(BasePlugin):
    """
    An MkDocs plugin that allows editing pages directly from the browser.
    
    This plugin provides a WebSocket server that communicates with client-side
    JavaScript to enable real-time editing of MkDocs pages without leaving
    the browser.
    """
    
    # Configuration schema
    config_scheme = (
        ('websockets_host', config_options.Type(str, default=None)),
        ('websockets_port', config_options.Type(int, default=8484)),
        ('websockets_timeout', config_options.Type(int, default=10)),
        ('debug_mode', config_options.Type(bool, default=False)),
        ('article_selector', config_options.Type(str, default=None)),
    )
    
    # HTML redirect template for page renames
    _REDIRECT_TEMPLATE = string.Template("""
        <!DOCTYPE html>
        <html>
            <head>
                <meta http-equiv="refresh" content="0; url=${new_url}" />
            </head>
        </html>
    """)
    
    def __init__(self):
        """Initialize the plugin and load client-side assets."""
        super().__init__()
        
        # Set up logging
        self.log: Logger = getLogger(f'mkdocs.plugins.{__name__}')
        
        # Load client-side assets
        self._load_client_assets()
        
        # Initialize state
        self.server_thread: Optional[threading.Thread] = None
        self.is_serving: bool = False
        self.mkdocs_config: Optional[MkDocsConfig] = None
        self.livereload_server: Optional[LiveReloadServer] = None
        
        # Track page redirects and new pages
        self.redirect_url: Optional[str] = None
        self.new_page_state: Dict[str, Any] = {
            "created_file": None,
            "new_url": None,
        }
    
    def _load_client_assets(self) -> None:
        """Load JavaScript and CSS assets for client-side functionality."""
        assets_dir = Path(__file__).parent
        
        # Load JavaScript
        js_file = assets_dir / 'live-edit.js'
        with open(js_file, 'r', encoding='utf-8') as file:
            self.js_contents = file.read()
        
        # Load CSS
        css_file = assets_dir / 'live-edit.css'
        with open(css_file, 'r', encoding='utf-8') as file:
            self.css_contents = file.read()
    
    # -------------------- File Operations --------------------
    
    def read_file_contents(self, path: str) -> str:
        """
        Read the contents of a page from the filesystem.
        
        Args:
            path: Relative path to the file within docs_dir
            
        Returns:
            The contents of the file as a string
        """
        file_path = Path(self.mkdocs_config['docs_dir']) / path
        with open(file_path, 'r', encoding='utf-8') as file:
            return file.read()
    
    def write_file_contents(self, path: str, contents: str) -> None:
        """
        Write contents to a file and trigger MkDocs rebuild.
        
        Args:
            path: Relative path to the file within docs_dir
            contents: New contents to write to the file
        """
        file_path = Path(self.mkdocs_config['docs_dir']) / path
        
        if self.config.get('debug_mode'):
            self.log.debug(f'Writing {len(contents)} chars to {file_path}')
        
        # Write the file
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(contents)
        
        # Trigger rebuild
        self._trigger_rebuild(file_path)
    
    def rename_file(self, old_filepath: str, new_filename: str) -> str:
        """
        Rename a file on the filesystem.
        
        Args:
            old_filepath: Current relative path to the file
            new_filename: New filename (not path)
            
        Returns:
            JSON string with operation result
        """
        try:
            docs_dir = Path(self.mkdocs_config['docs_dir'])
            old_path = docs_dir / old_filepath
            new_path = old_path.parent / new_filename
            
            # Perform the rename
            old_path.rename(new_path)
            
            # Calculate new URL
            new_file = File(
                str(new_path.relative_to(docs_dir)),
                self.mkdocs_config['docs_dir'],
                self.mkdocs_config['site_dir'],
                self.mkdocs_config['use_directory_urls']
            )
            new_page = Page(None, new_file, self.mkdocs_config)
            self.redirect_url = new_page.canonical_url
            
            return json.dumps({
                'action': 'rename_file',
                'success': True,
                'new_url': self.redirect_url
            })
            
        except Exception as error:
            self.log.error(f'Failed to rename {old_filepath} to {new_filename}: {error}')
            return json.dumps({
                'action': 'rename_file',
                'success': False,
                'error': str(error)
            })
    
    def delete_file(self, path: str) -> str:
        """
        Delete a file from the filesystem.
        
        Args:
            path: Relative path to the file within docs_dir
            
        Returns:
            JSON string with operation result
        """
        try:
            file_path = Path(self.mkdocs_config['docs_dir']) / path
            file_path.unlink()
            
            return json.dumps({
                'action': 'delete_file',
                'path': path,
                'success': True
            })
            
        except Exception as error:
            self.log.error(f'Failed to delete {path}: {error}')
            return json.dumps({
                'action': 'delete_file',
                'path': path,
                'success': False,
                'error': str(error)
            })
    
    def create_new_file(self, path: str, title: str) -> str:
        """
        Create a new file with initial content.
        
        Args:
            path: Relative path for the new file
            title: Title for the new page
            
        Returns:
            JSON string with operation result
        """
        try:
            new_path = Path(self.mkdocs_config['docs_dir']) / path
            
            # Ensure parent directory exists
            new_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write initial content
            with open(new_path, 'w', encoding='utf-8') as file:
                file.write(f'# {title}\n\nThis page was created using live-edit.')
            
            # Track for redirect after rebuild
            self.new_page_state["created_file"] = new_path
            
            return json.dumps({
                'action': 'new_file',
                'path': path,
                'success': True
            })
            
        except Exception as error:
            self.log.error(f'Failed to create {path}: {error}')
            return json.dumps({
                'action': 'new_file',
                'path': path,
                'success': False,
                'error': str(error)
            })
    
    # -------------------- Rebuild Triggering --------------------
    
    def _trigger_rebuild(self, file_path: Path) -> None:
        """
        Trigger MkDocs to rebuild the site after a file change.
        
        Args:
            file_path: Path to the file that was changed
        """
        if self.config.get('debug_mode'):
            self.log.debug(f'Triggering rebuild for {file_path}')
        
        # Try direct rebuild first
        if self._trigger_direct_rebuild():
            return
        
        # Fallback to filesystem-based triggers
        self._trigger_filesystem_rebuild(file_path)
    
    def _trigger_direct_rebuild(self) -> bool:
        """
        Directly trigger MkDocs rebuild via LiveReloadServer.
        
        Returns:
            True if successful, False otherwise
        """
        if not self.livereload_server:
            return False
        
        try:
            # Trigger rebuild
            with self.livereload_server._rebuild_cond:
                self.livereload_server._want_rebuild = True
                self.livereload_server._rebuild_cond.notify_all()
            
            # Schedule browser reload
            threading.Thread(
                target=self._trigger_browser_reload,
                daemon=True
            ).start()
            
            if self.config.get('debug_mode'):
                self.log.debug('Direct rebuild triggered successfully')
            
            return True
            
        except Exception as e:
            self.log.warning(f'Direct rebuild failed: {e}')
            return False
    
    def _trigger_browser_reload(self) -> None:
        """Trigger browser to reload after rebuild completes."""
        time.sleep(0.5)  # Allow time for rebuild
        
        try:
            with self.livereload_server._epoch_cond:
                self.livereload_server._visible_epoch = _timestamp()
                self.livereload_server._epoch_cond.notify_all()
                
        except Exception as e:
            if self.config.get('debug_mode'):
                self.log.debug(f'Browser reload signal failed: {e}')
    
    def _trigger_filesystem_rebuild(self, file_path: Path) -> None:
        """
        Trigger rebuild using filesystem events as fallback.
        
        Args:
            file_path: Path to the file that was changed
        """
        try:
            # Update file timestamps multiple times to ensure detection
            for i in range(3):
                time.sleep(0.1 * (i + 1))
                current_time = time.time() + i
                os.utime(file_path, (current_time, current_time))
                
        except Exception as e:
            self.log.warning(f'Filesystem trigger failed: {e}')
    
    # -------------------- WebSocket Server --------------------
    
    async def websocket_handler(self, websocket: websockets.ServerConnection) -> None:
        """
        Handle WebSocket connections and messages.
        
        Args:
            websocket: The WebSocket connection
        """
        if self.config.get('debug_mode'):
            self.log.debug('WebSocket client connected')
        
        # Send connection confirmation
        await websocket.send(json.dumps({
            'action': 'connected',
            'message': 'Live-edit WebSocket server connected'
        }))
        
        # Message handling loop
        while True:
            try:
                raw_message = await websocket.recv()
                message = json.loads(raw_message)
                
                response = await self._handle_websocket_message(message)
                if response:
                    await websocket.send(response)
                    
            except websockets.exceptions.ConnectionClosed:
                if self.config.get('debug_mode'):
                    self.log.debug('WebSocket client disconnected')
                break
                
            except json.JSONDecodeError as e:
                await websocket.send(json.dumps({
                    'action': 'error',
                    'message': f'Invalid JSON: {e}'
                }))
                
            except Exception as e:
                self.log.error(f'WebSocket error: {e}')
                break
    
    async def _handle_websocket_message(self, message: Dict[str, Any]) -> Optional[str]:
        """
        Handle a WebSocket message and return response.
        
        Args:
            message: Parsed message dictionary
            
        Returns:
            JSON response string or None
        """
        action = message.get('action')
        
        if self.config.get('debug_mode'):
            self.log.debug(f'Handling action: {action}')
        
        match action:
            case 'ready':
                # Client is ready, check for pending redirects
                if self.new_page_state.get("new_url"):
                    response = json.dumps({
                        'action': 'redirect',
                        'new_url': self.new_page_state["new_url"]
                    })
                    self.new_page_state["new_url"] = None
                    self.new_page_state["created_file"] = None
                    return response
                    
            case 'get_contents':
                contents = self.read_file_contents(message['path'])
                return json.dumps({
                    'action': 'get_contents',
                    'path': message['path'],
                    'contents': contents
                })
                
            case 'set_contents':
                try:
                    self.write_file_contents(message['path'], message['contents'])
                    return json.dumps({
                        'action': 'set_contents',
                        'path': message['path'],
                        'success': True
                    })
                except Exception as e:
                    return json.dumps({
                        'action': 'set_contents',
                        'path': message['path'],
                        'success': False,
                        'error': str(e)
                    })
                    
            case 'new_file':
                return self.create_new_file(message['path'], message['title'])
                
            case 'rename_file':
                return self.rename_file(message['path'], message['new_filename'])
                
            case 'delete_file':
                return self.delete_file(message['path'])
                
            case _:
                return json.dumps({
                    'action': 'error',
                    'message': f'Unknown action: {action}'
                })
        
        return None
    
    async def _run_websocket_server(self) -> None:
        """Run the WebSocket server event loop."""
        host = self.config.get('websockets_host') or '0.0.0.0'
        port = self.config.get('websockets_port')
        
        self.log.info(f'Starting WebSocket server on {host}:{port}')
        
        async with serve(self.websocket_handler, host, port):
            await asyncio.Future()  # Run forever
    
    def _websocket_server_thread(self) -> None:
        """WebSocket server thread entry point."""
        try:
            asyncio.run(self._run_websocket_server())
        except Exception as e:
            self.log.error(f'WebSocket server error: {e}')
    
    def _start_websocket_server(self) -> None:
        """Start the WebSocket server in a background thread."""
        if self.server_thread and self.server_thread.is_alive():
            return
        
        self.server_thread = threading.Thread(
            target=self._websocket_server_thread,
            daemon=True,
            name='LiveEditWebSocketServer'
        )
        self.server_thread.start()
        
        self.log.info('WebSocket server started')
    
    def _capture_livereload_server(self) -> None:
        """Attempt to capture reference to LiveReloadServer for direct rebuild."""
        def find_server():
            import gc
            time.sleep(1.5)  # Wait for server initialization
            
            for obj in gc.get_objects():
                if isinstance(obj, LiveReloadServer):
                    self.livereload_server = obj
                    if self.config.get('debug_mode'):
                        self.log.debug('LiveReloadServer reference captured')
                    break
        
        threading.Thread(target=find_server, daemon=True).start()
    
    # -------------------- MkDocs Plugin Hooks --------------------
    
    def on_config(self, config: MkDocsConfig, **kwargs) -> MkDocsConfig:
        """Store configuration for later use."""
        self.mkdocs_config = config
        return config
    
    def on_startup(self, *, command: Literal['build', 'gh-deploy', 'serve'], 
                   dirty: bool) -> None:
        """Initialize the plugin on MkDocs startup."""
        self.is_serving = (command == 'serve')
        
        if self.is_serving:
            self._capture_livereload_server()
            self._start_websocket_server()
    
    def on_serve(self, server: LiveReloadServer, /, *, 
                 config: MkDocsConfig, **kwargs) -> Optional[LiveReloadServer]:
        """Configure the live reload server."""
        # Store reference for direct rebuild
        self.livereload_server = server
        
        # Override error handler for redirect support
        original_handler = server.error_handler
        
        def custom_error_handler(code: int) -> Optional[bytes]:
            if code == 404 and self.redirect_url:
                # Redirect to renamed page
                response = self._REDIRECT_TEMPLATE.substitute(
                    new_url=self.redirect_url
                ).encode('utf-8')
                self.redirect_url = None
                return response
            return original_handler(code)
        
        server.error_handler = custom_error_handler
        
        return server
    
    def on_pre_page(self, page: Page, /, *, 
                    config: MkDocsConfig, files: Files) -> Optional[Page]:
        """Track new page URLs for redirect after creation."""
        if not self.new_page_state.get("created_file"):
            return page
        
        if self.new_page_state.get("new_url"):
            return page
        
        page_path = Path(config['docs_dir']) / page.file.src_path
        
        if page_path.samefile(self.new_page_state["created_file"]):
            self.new_page_state["new_url"] = page.abs_url
            self.log.info(f'New page created: {page.abs_url}')
        
        return page
    
    def on_page_content(self, html: str, /, *, 
                        page: Page, **kwargs) -> Optional[str]:
        """Inject live-edit client scripts into pages."""
        if not self.is_serving:
            return html
        
        # Prepare JavaScript variables
        page_info = {
            'ws_port': self.config.get('websockets_port'),
            'debug_mode': str(self.config.get('debug_mode', False)).lower(),
            'page_path': page.file.src_uri,
            'page_filename': os.path.basename(page.file.src_path),
            'page_base_path': Path(page.file.src_path).parent.as_posix(),
            'article_selector': self.config.get('article_selector') or 'null'
        }
        
        # Build script preamble
        preamble = '\n'.join([
            f"const ws_port = {page_info['ws_port']};",
            f"const debug_mode = {page_info['debug_mode']};",
            f"let page_path = '{page_info['page_path']}';",
            f"let page_filename = '{page_info['page_filename']}';",
            f"let page_base_path = '{page_info['page_base_path']}';",
            f"let article_selector = {page_info['article_selector']};"
        ])
        
        # Inject CSS and JavaScript
        return (
            f'<style>{self.css_contents}</style>\n'
            f'{html}'
            f'<script>\n{preamble}\n{self.js_contents}\n</script>'
        )
