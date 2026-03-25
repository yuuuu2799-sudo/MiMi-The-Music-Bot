import discord
from discord.ext import commands
from discord import app_commands
import wavelink
import asyncio
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# --- MONKEY PATCH WAVELINK FOR LAVALINK V4.0.0+ (DAVE) ---
# Lavalink v4 and newer requires 'channelId' in the VoiceState data. Wavelink 3.4.1 does not send it.
async def _patched_dispatch_voice_update(self) -> None:
    assert self.guild is not None
    data = self._voice_state["voice"]

    session_id = data.get("session_id", None)
    token = data.get("token", None)
    endpoint = data.get("endpoint", None)

    if not session_id or not token or not endpoint:
        return

    # FIX: Provide channelId inside voice state request
    channel_id = str(self.channel.id) if self.channel else None

    # Wavelink internally expects dict for the request
    request = {"voice": {"sessionId": session_id, "token": token, "endpoint": endpoint, "channelId": channel_id}}

    try:
        await self.node._update_player(self.guild.id, data=request)
    except Exception:
        await self.disconnect()
    else:
        self._connection_event.set()

wavelink.Player._dispatch_voice_update = _patched_dispatch_voice_update
# --- END MONKEY PATCH ---
class MusicBot(commands.Bot):
    def __init__(self):
        # FIX FOR THE TIMEOUT ERROR:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True  # Required for voice connection
        intents.guilds = True        # Required for server interaction
        
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Connect to the public Lavalink node provided
        nodes = [wavelink.Node(uri="https://lavalinkv4.serenetia.com:443", password="https://dsc.gg/ajidevserver")]
        await wavelink.Pool.connect(nodes=nodes, client=self, cache_capacity=100)

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        # Syncing slash commands so they show up in Discord
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash commands.")
        except Exception as e:
            print(f"Error syncing commands: {e}")
        print("Lavalink Node Connected & Ready!")

bot = MusicBot()


# --- Helpers ---
def create_embed(title, description, color=discord.Color.blue()):
    return discord.Embed(title=title, description=description, color=color)

# --- Music Controls UI ---
class PlayerControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Play/Resume", style=discord.ButtonStyle.success, emoji="▶️")
    async def play_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc: return await interaction.response.send_message("Not connected.", ephemeral=True)
        if vc.paused:
            await vc.pause(False)
            await interaction.response.send_message("Resumed ▶️", ephemeral=True)
        else:
            await interaction.response.send_message("Already playing.", ephemeral=True)

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="⏸️")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc: return await interaction.response.send_message("Not connected.", ephemeral=True)
        if not vc.paused:
            await vc.pause(True)
            await interaction.response.send_message("Paused ⏸️", ephemeral=True)
        else:
            await interaction.response.send_message("Already paused.", ephemeral=True)

    @discord.ui.button(label="Skip/Next", style=discord.ButtonStyle.primary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc: wavelink.Player = interaction.guild.voice_client
        if vc and vc.playing:
            await vc.skip()
            await interaction.response.send_message("Skipped to next song! ⏭️", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, emoji="🔁")
    async def loop_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc: wavelink.Player = interaction.guild.voice_client
        if not vc: return await interaction.response.send_message("Not connected.", ephemeral=True)
        if vc.queue.mode == wavelink.QueueMode.loop:
            vc.queue.mode = wavelink.QueueMode.normal
            await interaction.response.send_message("Looping disabled. ➡️", ephemeral=True)
        else:
            vc.queue.mode = wavelink.QueueMode.loop
            await interaction.response.send_message("Looping enabled! 🔁", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc: wavelink.Player = interaction.guild.voice_client
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("Stopped and Disconnected.", ephemeral=True)
        else:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


# --- Slash Commands ---

@bot.tree.command(name="play", description="Play a song or playlist from YouTube")
@app_commands.describe(search="The song name or YouTube link")
async def play(interaction: discord.Interaction, search: str):
    if not interaction.user.voice:
        return await interaction.response.send_message("You must be in a voice channel!", ephemeral=True)

    await interaction.response.defer()

    # Ensure we are connected
    if not interaction.guild.voice_client:
        try:
            vc: wavelink.Player = await interaction.user.voice.channel.connect(cls=wavelink.Player)
        except asyncio.TimeoutError:
            return await interaction.followup.send("Connection timed out. Check if 'Voice State Intent' is ON in Dev Portal.")
    else:
        vc: wavelink.Player = interaction.guild.voice_client

    # Explicitly search YouTube using the plugin
    tracks = await wavelink.Playable.search(search, source=wavelink.TrackSource.YouTube)
    
    if not tracks:
        return await interaction.followup.send("No results found on YouTube.")

    if isinstance(tracks, wavelink.Playlist):
        await vc.queue.put_wait(tracks)
        await interaction.followup.send(f"Added playlist **{tracks.name}** ({len(tracks.tracks)} songs) to queue.", view=PlayerControls())
    else:
        track = tracks[0]
        await vc.queue.put_wait(track)
        await interaction.followup.send(f"Added **{track.title}** to queue.", view=PlayerControls())

    if not vc.playing:
        await vc.play(vc.queue.get())


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if vc and vc.playing:
        await vc.skip()
        await interaction.response.send_message("Skipped! ⏭️")
    else:
        await interaction.response.send_message("Nothing is playing.")


@bot.tree.command(name="pause", description="Pause the music")
async def pause(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if vc:
        await vc.pause(True)
        await interaction.response.send_message("Paused ⏸️")

@bot.tree.command(name="resume", description="Resume the music")
async def resume(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if vc:
        await vc.pause(False)
        await interaction.response.send_message("Resumed ▶️")

@bot.tree.command(name="nowplaying", description="Show the current song")
async def nowplaying(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or not vc.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    
    track = vc.current
    embed = create_embed("Now Playing", f"[{track.title}]({track.uri})")
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    await interaction.response.send_message(embed=embed, view=PlayerControls())

@bot.tree.command(name="queue", description="Show the current song queue")
async def queue(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc or vc.queue.is_empty:
        return await interaction.response.send_message("The queue is empty.", ephemeral=True)

    upcoming = list(vc.queue)[:10]
    queue_list = "\n".join(f"{i+1}. {t.title}" for i, t in enumerate(upcoming))
    await interaction.response.send_message(embed=create_embed("Current Queue", queue_list))

@bot.tree.command(name="stop", description="Stop music and disconnect")
async def stop(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Stopped and Disconnected.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.")

@bot.tree.command(name="loop", description="Toggle loop mode")
async def loop(interaction: discord.Interaction):
    vc: wavelink.Player = interaction.guild.voice_client
    if not vc: return await interaction.response.send_message("Not connected.")

    if vc.queue.mode == wavelink.QueueMode.loop:
        vc.queue.mode = wavelink.QueueMode.normal
        await interaction.response.send_message("Looping disabled.")
    else:
        vc.queue.mode = wavelink.QueueMode.loop
        await interaction.response.send_message("Looping enabled 🔁")

# --- Auto-play next song ---
@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    player = payload.player
    if not player: return
    if not player.queue.is_empty:
        next_track = player.queue.get()
        await player.play(next_track)

bot.run(os.getenv('DISCORD_TOKEN'))