'''
Created on feb. 20 2014

@author: Flavian
'''

import sys
import logging
logger = logging.getLogger(__name__)
import time
import numpy as np

class Decode:
    def __init__(self, location, amp=0., pitch=0, stretch=0, ogg_quality=-1,
                 eq={'band0': 0.0, 'band1': 0.0, 'band2': 0.0},
                 mode='appsink', location_store=u"", url=False):
        """
        location: filename in unicode
        amp: Amplitude in dB
        pitch: in percent
        stretch: in percent
        ogg_quality: 0 is not compressed, between (0, 1] it is compressed,
        1 being the best quality.
        mode='appsink', 'filesink' or 'filewavsink'
        """
        import pygst
        pygst.require('0.10')
        import gst
        import gobject
        gobject.threads_init()

        self.callback_list = []
        # The pipeline
        self.pipeline = gst.Pipeline()
        self.buffer = None
        self.position = None
        self.list_buffers = []
        self.sr = 11025
        self.memory = None
        # Create bus and connect several handlers
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect('message::eos', self.on_eos)
        self.bus.connect('message::tag', self.on_tag)
        self.bus.connect('message::error', self.on_error)
        #Assert the location is unicode iff it's a path
        if not url:
            assert isinstance(location, unicode)
        if url:
            #See https://gist.github.com/yokamaru/850506
            self.dec = gst.element_factory_make('uridecodebin')
            self.dec.set_property('uri', location)
            self.pipeline.add(self.dec)
            self.dec.connect('pad-added', self.on_pad_added)
        else:
            self.src = gst.element_factory_make('filesrc')
            # Set 'location' property on filesrc
            self.src.set_property('location', location.encode('utf-8'))
            self.dec = gst.element_factory_make('decodebin2')
            self.pipeline.add(self.src, self.dec)
            self.src.link(self.dec)
            self.dec.connect('new-decoded-pad', self.on_new_decoded_pad)
        self.conv = gst.element_factory_make('audioconvert')
        self.pitch = gst.element_factory_make("pitch")
        self.pitch.set_property("pitch", 1 + 0.01 * pitch)
        self.pitch.set_property("tempo", 1 + 0.01 * stretch)
        audioamplify = gst.element_factory_make("audioamplify")
        audioamplify.set_property("amplification",
                                  10 ** (amp / 10.))
        self.conv2 = gst.element_factory_make('audioconvert')
        self.rsmpl = gst.element_factory_make('audioresample')
        self.rsmpl.props.quality = 10
        self.capsfilter = gst.element_factory_make('capsfilter', 'capsfilter')
        self.appsink = gst.element_factory_make('appsink', 'sink')

        # Connect handler for 'new-decoded-pad' signal
        eq_el = gst.element_factory_make("equalizer-3bands", "eq")
        eq_el.set_property("band0", eq["band0"])
        eq_el.set_property("band1", eq["band1"])
        eq_el.set_property("band2", eq["band2"])
        # Add elements to pipeline
        self.pipeline.add(self.conv, self.pitch, eq_el,
                          audioamplify, self.conv2)

        # Link *some* elements
        # This is completed in self.on_new_decoded_pad()
        self.conv.link(self.pitch)
        self.pitch.link(audioamplify)
        audioamplify.link(eq_el)

        if ogg_quality > 0:
            assert ogg_quality <= 1,\
                   "The quality must be less than or equal to 1."
            self.encoder = gst.element_factory_make("vorbisenc")
            self.encoder.set_property("quality", ogg_quality)
            self.decoder = gst.element_factory_make("vorbisdec")
            self.pipeline.add(self.encoder, self.decoder)
            gst.element_link_many(eq_el, self.encoder, self.decoder,
                                  self.conv2)
        else:
            gst.element_link_many(eq_el, self.conv2)
        # Reference used in self.on_new_decoded_pad()
        self.apad = self.conv.get_pad('sink')

        # -- setup converter --
        # Little endian 16-bit signed integers B-)

        #/!\CAREFUL, ECHOPRINT ONLY WORKS WITH A SAMPLE RATE OF 11025
        caps = gst.Caps('audio/x-raw-int, channels=1, endianness=1234, '\
                        'width=16, depth=16, rate=11025, signed=true')
        self.capsfilter.set_property("caps", caps)

        if mode == 'appsink':
            # -- setup appsink --
            # this makes appsink emit signals
            self.appsink.set_property('emit-signals', True)
            # turns off sync to make decoding as fast as possible
            self.appsink.set_property('sync', False)
            self.appsink.connect('new-buffer', self.on_new_buffer)
            self.appsink.connect('new-preroll', self.on_new_preroll)
            self.pipeline.add(self.rsmpl, self.capsfilter, self.appsink)
            gst.element_link_many(self.conv2, self.rsmpl, self.capsfilter, self.appsink)
        elif mode == 'filesink':
            self.encoder = gst.element_factory_make("vorbisenc")
            self.encoder.set_property("quality", 1)
            self.mux = gst.element_factory_make("oggmux")
            self.filesink = gst.element_factory_make("filesink")
            self.filesink.set_property("location", location_store)
            self.conv3 = gst.element_factory_make("audioconvert")
            self.pipeline.add(self.rsmpl, self.capsfilter, self.conv3, self.encoder, self.mux, self.filesink)
            gst.element_link_many(self.conv2, self.rsmpl, self.capsfilter, self.conv3, self.encoder,
                                  self.mux, self.filesink)
        elif mode == 'filewavsink':
            self.encoder = gst.element_factory_make("wavenc")
            self.filesink = gst.element_factory_make("filesink")
            self.filesink.set_property("location", location_store)
            self.conv3 = gst.element_factory_make("audioconvert")
            self.pipeline.add(self.rsmpl, self.capsfilter, self.conv3, self.encoder, self.filesink)
            gst.element_link_many(self.conv2, self.rsmpl, self.capsfilter, self.conv3, self.encoder, self.filesink)

    def start(self):
        import gst
        import gobject

        # The MainLoop
        self.mainloop = gobject.MainLoop()
        # And off we go!
        self.pipeline.set_state(gst.STATE_PLAYING)
        self.mainloop.run()

    def on_new_buffer(self, appsink):
        buf = appsink.emit('pull-buffer')
        np_buf = np.fromstring(buf, dtype=np.int16)
        if self.buffer is None:
            self.buffer = np_buf
        else:
            self.buffer = np.concatenate((self.buffer, np_buf))
        for callback in self.callback_list:
            self.buffer, self.memory = callback(self.buffer, self.memory)

    def on_new_preroll(self, appsink):
        buf = appsink.emit('pull-preroll')
        #print 'new preroll', len(buf)

    def on_pad_added(self, element, pad):
        caps = pad.get_caps()
        name = caps[0].get_name()
        if name == 'audio/x-raw-float' or name == 'audio/x-raw-int':
            if not self.apad.is_linked(): # Only link once
                pad.link(self.apad)

    def on_new_decoded_pad(self, element, pad, last):
        import gst

        caps = pad.get_caps()
        name = caps[0].get_name()
        #"print 'on_new_decoded_pad:', name
        if name == 'audio/x-raw-float' or name == 'audio/x-raw-int':
            if not self.apad.is_linked():  # Only link once
                pad.link(self.apad)
        format = gst.Format(gst.FORMAT_BUFFERS)
        try:
            duration = self.pipeline.query_duration(format)[0]
            self.buffer = np.zeros(duration, dtype=np.int16)
            self.position = 0
        except:
            pass

    def on_eos(self, bus, msg):
        import gst
        self.pipeline.set_state(gst.STATE_NULL)
        self.mainloop.quit()

    def on_tag(self, bus, msg):
#         taglist = msg.parse_tag()
#         print 'on_tag:'
#         for key in taglist.keys():
#             print '\t%s = %s' % (key, taglist[key])
        pass

    def on_error(self, bus, msg):
        error = msg.parse_error()
        self.mainloop.quit()
        raise IOError(u"GStreamer error: {}".format(error[1]))

    def get_data(self, t0=0, t1=None):
        if self.buffer is None:
            self.buffer = np.concatenate(self.list_buffers)
        t1 = float(len(self.buffer)) / self.sr if t1 == None else t1
        return self.get_raw_data(t0 * self.sr, t1 * self.sr)

    def get_raw_data(self, start=0, end=None):
        if start < 0:
            start = 0
        if self.buffer is None:
            self.buffer = np.concatenate(self.list_buffers)
        end = len(self.buffer) if end == None else end
        return self.buffer[start:end]

    def get_total_length(self):
        if self.buffer is None:
            self.buffer = np.concatenate(self.list_buffers)
        return float(len(self.buffer)) / self.sr
    
    def add_callback(self, callback):
        self.callback_list.append(callback)


import wave

def decode_wave(infilename, buf_start=0, buf_end=None, samplerate_assert=None):
    """
    Decode wave files between frame buf_start and buf_end (not included).
    """
    infile = wave.open(infilename, "rb")
    width = infile.getsampwidth()
    rate = infile.getframerate()
    n_frames = infile.getnframes()
    buf_end = buf_end if buf_end is not None else n_frames
    if samplerate_assert is not None:
        assert rate == samplerate_assert
    assert width == 2, "Invalid wave: width must by 2 bytes"
    if buf_end > n_frames:
        logger.warning("buf_end must be lower than number of frames. Setting it to max frame.")
        buf_end = n_frames
    if buf_start >= n_frames:
        logger.warning("empty buffer")
        return []
    if buf_start < 0:
        buf_start = 0
    length = buf_end - buf_start
    anchor = infile.tell()
    infile.setpos(anchor + buf_start)
    data = np.fromstring(infile.readframes(length), dtype=np.int16)
    infile.close()
    return data, buf_end == n_frames

def length_wave(infilename):
    """
    Returns the length in seconds of a wave file.
    """
    infile = wave.open(infilename, "rb")
    rate = infile.getframerate()
    n_frames = infile.getnframes()
    return float(n_frames) / rate

# For testing
if __name__ == '__main__':
    start = time.time()
    d = Decode("C:\\christine.wav")
    import matplotlib.pyplot as plt
    a = d.get_data()
    plt.plot(a)
    plt.show()
