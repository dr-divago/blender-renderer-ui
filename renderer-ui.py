import bpy
import os
import tempfile
import threading
import time
import requests
from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, EnumProperty
from bpy.types import Panel, Operator, PropertyGroup

bl_info = {
    "name": "Remote Render - Server Integration",
    "author": "Matteo Pelliccia",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "Properties > Render Properties > Remote Render",
    "description": "Submit renders to render server and get notified when complete",
    "category": "Render",
}

class RemoteRenderJob(PropertyGroup):
    job_id: StringProperty(name="Job ID")
    scene_name: StringProperty(name="Scene Name")
    submitted_time: StringProperty(name="Submitted")
    status: StringProperty(name="Status", default="SUBMITTED")
    progress: IntProperty(name="Progress", default=0, min=0, max=100, subtype='PERCENTAGE')
    is_complete: BoolProperty(name="Complete", default=False)
    has_notification: BoolProperty(name="New", default=True)  # For showing notification badge
    error_message: StringProperty(name="Error", default="")

class ServerIntegratedSettings(PropertyGroup):
    server_url: StringProperty(
        name="Server URL",
        description="Remote render server URL",
        default="http://localhost:8080"
    )

    start_frame: IntProperty(
        name="Start Frame",
        description="First frame to render",
        default=1,
        min=1
    )

    end_frame: IntProperty(
        name="End Frame",
        description="Last frame to render",
        default=1,
        min=1
    )

    jobs: CollectionProperty(type=RemoteRenderJob)

    show_notifications: BoolProperty(
        name="Desktop Notifications",
        description="Show system notifications when renders complete",
        default=True
    )

    render_quality: EnumProperty(
        name="Quality Preset",
        description="Choose render quality preset",
        items=[
            ('CUSTOM', "Custom Settings", "Use custom render settings"),
            ('FAST', "Fast (Low Quality)", "Fast render with low quality for previews - 64 samples, 4 bounces"),
            ('HIGH', "High Quality", "High quality render for final output - 1024 samples, 12 bounces")
        ],
        default='FAST'
    )

    server_status: StringProperty(name="Server Status", default="Unknown")
    last_check: StringProperty(name="Last Check", default="")

class INTEGRATED_OT_test_connection(Operator):
    """Test connection to render server"""
    bl_idname = "integrated.test_connection"
    bl_label = "Test Connection"
    bl_description = "Check if render server is accessible"

    def execute(self, context):
        settings = context.scene.server_integrated_settings

        try:
            response = requests.get(f"{settings.server_url}/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                settings.server_status = "Connected ✓"
                settings.last_check = time.strftime("%H:%M:%S")
                self.report({'INFO'}, f"Server connection successful! Status: {data.get('status', 'unknown')}")
            else:
                settings.server_status = f"Error {response.status_code}"
                self.report({'ERROR'}, f"Server returned status: {response.status_code}")
        except requests.exceptions.ConnectionError:
            settings.server_status = "Connection Failed"
            self.report({'ERROR'}, "Could not connect to server. Is it running?")
        except requests.exceptions.Timeout:
            settings.server_status = "Timeout"
            self.report({'ERROR'}, "Connection timeout")
        except Exception as e:
            settings.server_status = "Error"
            self.report({'ERROR'}, f"Connection failed: {str(e)}")

        return {'FINISHED'}

class INTEGRATED_OT_submit_render(Operator):
    """Submit current scene for remote rendering"""
    bl_idname = "integrated.submit_render"
    bl_label = "Render on Server"
    bl_description = "Submit current scene to server and continue working"

    def execute(self, context):
        settings = context.scene.server_integrated_settings

        try:
            self.report({'INFO'}, "Preparing scene for upload...")
            temp_blend_file = self.save_scene_to_temp()

            self.report({'INFO'}, "Uploading scene to server...")
            upload_result = self.upload_file(settings.server_url, temp_blend_file)
            if not upload_result:
                return {'CANCELLED'}

            self.report({'INFO'}, "Submitting render job...")
            job_result = self.submit_render_job(settings, upload_result)
            if not job_result:
                return {'CANCELLED'}

            new_job = settings.jobs.add()
            new_job.job_id = job_result.get('id', 'unknown')
            new_job.scene_name = self.get_scene_name()
            new_job.submitted_time = time.strftime("%H:%M")
            new_job.status = "SUBMITTED"

            monitor_thread = threading.Thread(target=self.monitor_job, args=(context, new_job.job_id))
            monitor_thread.daemon = True
            monitor_thread.start()

            self.report({'INFO'}, f"Render submitted successfully! Job ID: {new_job.job_id}")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"Error submitting job: {str(e)}")
            return {'CANCELLED'}

    def save_scene_to_temp(self):
        """Save current scene to a temporary .blend file"""
        temp_dir = tempfile.gettempdir()
        temp_filename = f"remote_render_{int(time.time())}.blend"
        temp_path = os.path.join(temp_dir, temp_filename)

        bpy.ops.wm.save_as_mainfile(filepath=temp_path, copy=True)
        return temp_path

    def upload_file(self, server_url, file_path):
        """Upload .blend file to server"""
        try:
            with open(file_path, 'rb') as f:
                files = {'file': (os.path.basename(file_path), f, 'application/octet-stream')}
                response = requests.post(f"{server_url}/api/files/upload", files=files, timeout=60)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                self.report({'ERROR'}, f"Upload failed: {response.status_code}")
                return None

        except Exception as e:
            self.report({'ERROR'}, f"Upload error: {str(e)}")
            return None
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

    def submit_render_job(self, settings, upload_result):
        """Submit render job to server"""
        scene = bpy.context.scene
        
        # Check if using quality preset or custom settings
        if settings.render_quality in ['FAST', 'HIGH']:
            # Use the new preset endpoint
            job_data = {
                "fileName": upload_result.get('fileName', 'unknown.blend'),
                "quality": settings.render_quality,
                "startFrame": settings.start_frame,
                "endFrame": settings.end_frame,
                "resolutionX": scene.render.resolution_x,
                "resolutionY": scene.render.resolution_y,
                "outputFormat": "PNG",
                "description": f"Blender render job from {self.get_scene_name()} ({settings.render_quality} quality)"
            }
            
            endpoint = f"{settings.server_url}/api/presets/render"
            
        else:
            # Use the original custom settings endpoint
            job_data = {
                "fileName": upload_result.get('fileName', 'unknown.blend'),
                "renderSettings": {
                    "startFrame": settings.start_frame,
                    "endFrame": settings.end_frame,
                    "resolutionX": scene.render.resolution_x,
                    "resolutionY": scene.render.resolution_y,
                    "samples": scene.cycles.samples if scene.render.engine == 'CYCLES' else None,
                    "outputFormat": "PNG",
                    "renderEngine": scene.render.engine,
                    "deviceType": "AUTO"
                },
                "description": f"Blender render job from {self.get_scene_name()}"
            }
            
            endpoint = f"{settings.server_url}/api/jobs"

        try:
            response = requests.post(
                endpoint,
                json=job_data,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )

            if response.status_code in [200, 201]:
                return response.json()
            else:
                self.report({'ERROR'}, f"Job submission failed: {response.status_code}")
                return None

        except Exception as e:
            self.report({'ERROR'}, f"Job submission error: {str(e)}")
            return None

    def get_scene_name(self):
        """Get a friendly name for the current scene"""
        if bpy.data.filepath:
            return os.path.basename(bpy.data.filepath)
        else:
            return "Untitled.blend"

    def monitor_job(self, context, job_id):
        """Monitor job progress in background thread"""
        settings = context.scene.server_integrated_settings

        while True:
            try:
                job = None
                for j in settings.jobs:
                    if j.job_id == job_id:
                        job = j
                        break

                if not job:
                    break  # Job was removed

                response = requests.get(f"{settings.server_url}/api/jobs/{job_id}", timeout=10)
                if response.status_code == 200:
                    job_data = response.json()

                    new_status = job_data.get('status', 'UNKNOWN')
                    job.status = new_status
                    job.progress = int(job_data.get('progress', 0))

                    if 'errorMessage' in job_data and job_data['errorMessage']:
                        job.error_message = job_data['errorMessage']

                    def update_ui():
                        try:
                            for area in bpy.context.screen.areas:
                                area.tag_redraw()
                        except:
                            pass

                    bpy.app.timers.register(update_ui, first_interval=0.1)

                    if new_status in ['COMPLETED', 'FAILED']:
                        job.is_complete = True
                        job.has_notification = True

                        if new_status == 'COMPLETED' and settings.show_notifications:
                            self.show_completion_notification(job.scene_name)
                        elif new_status == 'FAILED':
                            self.show_failure_notification(job.scene_name, job.error_message)

                        bpy.app.timers.register(update_ui, first_interval=0.1)
                        break

                time.sleep(5)

            except Exception:
                time.sleep(10)

    def show_completion_notification(self, scene_name):
        """Show system notification for completion"""
        try:
            import plyer
            plyer.notification.notify(
                title="Render Complete! 🎉",
                message=f"Scene '{scene_name}' has finished rendering",
                timeout=10
            )
        except ImportError:
            self.show_blender_notification('INFO', f"🎉 Render Complete: {scene_name}")

    def show_failure_notification(self, scene_name, error_message):
        """Show system notification for failure"""
        try:
            import plyer
            plyer.notification.notify(
                title="Render Failed ❌",
                message=f"Scene '{scene_name}' failed: {error_message[:50]}...",
                timeout=10
            )
        except ImportError:
            self.show_blender_notification('ERROR', f"❌ Render Failed: {scene_name} - {error_message[:50]}")

    def show_blender_notification(self, level, message):
        """Show notification in Blender's interface"""
        def show_notification():
            try:
                bpy.ops.wm.report({level}, message)
            except:
                print(f"[{level}] {message}")

        bpy.app.timers.register(show_notification, first_interval=0.1)

class INTEGRATED_OT_download_job(Operator):
    """Download completed render"""
    bl_idname = "integrated.download_job"
    bl_label = "Download"
    bl_description = "Download rendered images"

    job_id: StringProperty()

    def execute(self, context):
        settings = context.scene.server_integrated_settings

        try:
            response = requests.get(f"{settings.server_url}/api/files/download/{self.job_id}", timeout=60)

            if response.status_code == 200:
                download_dir = self.get_download_directory()
                filename = f"render_job_{self.job_id}.zip"
                filepath = os.path.join(download_dir, filename)

                with open(filepath, 'wb') as f:
                    f.write(response.content)

                for job in settings.jobs:
                    if job.job_id == self.job_id:
                        job.has_notification = False
                        break

                self.report({'INFO'}, f"Results downloaded to: {filepath}")

                if os.name == 'nt':  # Windows
                    os.startfile(download_dir)
                elif os.name == 'posix':  # macOS and Linux
                    os.system(f'open "{download_dir}"' if os.uname().sysname == 'Darwin' else f'xdg-open "{download_dir}"')

            else:
                self.report({'ERROR'}, f"Download failed: {response.status_code}")

        except Exception as e:
            self.report({'ERROR'}, f"Download error: {str(e)}")

        return {'FINISHED'}

    def get_download_directory(self):
        """Get appropriate download directory"""
        if bpy.data.filepath:
            return os.path.dirname(bpy.data.filepath)
        else:
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            if os.path.exists(downloads):
                return downloads
            else:
                return os.path.expanduser("~")

class INTEGRATED_OT_clear_job(Operator):
    """Remove job from list"""
    bl_idname = "integrated.clear_job"
    bl_label = "Clear"
    bl_description = "Remove job from list"

    job_index: IntProperty()

    def execute(self, context):
        settings = context.scene.server_integrated_settings
        settings.jobs.remove(self.job_index)
        return {'FINISHED'}

class INTEGRATED_OT_clear_all_jobs(Operator):
    """Clear all completed jobs"""
    bl_idname = "integrated.clear_all_jobs"
    bl_label = "Clear All Completed"
    bl_description = "Remove all completed jobs from list"

    def execute(self, context):
        settings = context.scene.server_integrated_settings
        for i in range(len(settings.jobs) - 1, -1, -1):
            if settings.jobs[i].is_complete:
                settings.jobs.remove(i)
        return {'FINISHED'}

class INTEGRATED_OT_refresh_ui(Operator):
    """Refresh UI display"""
    bl_idname = "integrated.refresh_ui"
    bl_label = "Refresh"
    bl_description = "Refresh the UI display"

    def execute(self, context):
        for area in bpy.context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}

class INTEGRATED_PT_remote_render(Panel):
    """Server integrated remote render panel"""
    bl_label = "Remote Render"
    bl_idname = "INTEGRATED_PT_remote_render"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.server_integrated_settings

        self.draw_server_section(layout, settings)
        layout.separator()

        self.draw_render_settings(layout, settings)
        layout.separator()

        layout.operator("integrated.submit_render", icon='RENDER_ANIMATION', text="Render on Server")
        layout.separator()

        self.draw_jobs_section(layout, settings)

    def draw_server_section(self, layout, settings):
        """Draw server connection section"""
        box = layout.box()
        box.label(text="Server Connection", icon='WORLD')

        box.prop(settings, "server_url")

        status_row = box.row()
        status_row.operator("integrated.test_connection", icon='PLUGIN')

        if settings.server_status:
            status_col = status_row.column()
            if "✓" in settings.server_status:
                status_col.label(text=settings.server_status, icon='CHECKMARK')
            else:
                status_col.alert = True
                status_col.label(text=settings.server_status, icon='ERROR')

            if settings.last_check:
                box.label(text=f"Last check: {settings.last_check}")

    def draw_render_settings(self, layout, settings):
        """Draw render settings section"""
        box = layout.box()
        box.label(text="Render Settings", icon='SETTINGS')

        # Quality preset selection
        quality_row = box.row()
        quality_row.prop(settings, "render_quality", expand=False)
        
        # Show quality preset info
        if settings.render_quality == 'FAST':
            info_box = box.box()
            info_box.label(text="Fast Preset:", icon='INFO')
            info_box.label(text="• 64 samples, 4 light bounces")
            info_box.label(text="• Optimized for speed and previews")
        elif settings.render_quality == 'HIGH':
            info_box = box.box()
            info_box.label(text="High Quality Preset:", icon='INFO')
            info_box.label(text="• 1024 samples, 12 light bounces")
            info_box.label(text="• Maximum quality for final renders")

        box.separator()

        row = box.row()
        row.prop(settings, "start_frame")
        row.prop(settings, "end_frame")

        scene = bpy.context.scene
        info_row = box.row()
        info_row.label(text=f"Resolution: {scene.render.resolution_x}×{scene.render.resolution_y}")
        info_row = box.row()
        info_row.label(text=f"Engine: {scene.render.engine}")

        box.prop(settings, "show_notifications")

    def draw_jobs_section(self, layout, settings):
        """Draw the jobs tracking section"""
        jobs_box = layout.box()

        header_row = jobs_box.row()
        header_row.label(text="Render Jobs", icon='RENDER_RESULT')
        header_row.operator("integrated.refresh_ui", icon='FILE_REFRESH', text="")

        completed_count = sum(1 for job in settings.jobs if job.is_complete)
        pending_count = len(settings.jobs) - completed_count
        new_count = sum(1 for job in settings.jobs if job.has_notification)

        if new_count > 0:
            badge_row = jobs_box.row()
            badge_row.alert = True
            badge_row.label(text=f"🔴 {new_count} new")

        if len(settings.jobs) > 0:
            summary_row = jobs_box.row()
            summary_row.label(text=f"Pending: {pending_count} | Completed: {completed_count}")

        if len(settings.jobs) == 0:
            jobs_box.label(text="No render jobs yet", icon='INFO')
        else:
            for i, job in enumerate(settings.jobs):
                self.draw_job_item(jobs_box, job, i)

        if completed_count > 0:
            jobs_box.operator("integrated.clear_all_jobs", icon='TRASH')

    def draw_job_item(self, layout, job, index):
        """Draw individual job item"""
        job_box = layout.box()

        if job.has_notification:
            job_box.alert = True  # Highlight new completions

        info_row = job_box.row()

        if job.is_complete:
            if job.status == 'COMPLETED':
                info_row.label(text="", icon='CHECKMARK')
            else:  # FAILED
                info_row.label(text="", icon='CANCEL')
        else:
            info_row.label(text="", icon='TIME')

        info_row.label(text=f"{job.scene_name} ({job.submitted_time})")

        status_row = job_box.row()
        status_row.label(text=f"Status: {job.status}")

        if not job.is_complete and job.progress > 0:
            progress_row = job_box.row()
            progress_row.prop(job, "progress", text="Progress", slider=True)

        if job.error_message:
            error_row = job_box.row()
            error_row.alert = True
            error_row.label(text=f"Error: {job.error_message[:50]}...")

        actions_row = job_box.row()
        if job.is_complete and job.status == 'COMPLETED':
            download_op = actions_row.operator("integrated.download_job", icon='IMPORT', text="Download")
            download_op.job_id = job.job_id

        # Clear button
        clear_op = actions_row.operator("integrated.clear_job", icon='X', text="Remove")
        clear_op.job_index = index

classes = [
    RemoteRenderJob,
    ServerIntegratedSettings,
    INTEGRATED_OT_test_connection,
    INTEGRATED_OT_submit_render,
    INTEGRATED_OT_download_job,
    INTEGRATED_OT_clear_job,
    INTEGRATED_OT_clear_all_jobs,
    INTEGRATED_OT_refresh_ui,
    INTEGRATED_PT_remote_render,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.server_integrated_settings = bpy.props.PointerProperty(type=ServerIntegratedSettings)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.server_integrated_settings

if __name__ == "__main__":
    register()