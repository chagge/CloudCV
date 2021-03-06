__author__ = 'clint'

from django.views.generic import CreateView, DeleteView
from django.http import HttpResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt

from app.models import Picture, Classify
from celeryTasks.webTasks.classifyTask import classifyImages
from cloudcv17 import config
from os.path import basename
import app.conf as conf

from PIL import Image
from urlparse import urlparse
from querystring_parser import parser

import time
import os
import json
import traceback
import shortuuid
import sys
import redis

redis_obj = redis.StrictRedis(host=config.REDIS_HOST, port=6379, db=0)
classify_channel_name = 'classify_queue'

# SET OF PATH CONSTANTS - SOME UNUSED
# File initially downloaded here
download_directory = conf.PIC_DIR
# Input image is saved here (symbolic links) - after resizing to 500 x 500
physical_job_root = conf.LOCAL_CLASSIFY_JOB_DIR
demo_log_file = physical_job_root + 'classify_demo.log'


def log_to_terminal(message, socketid):
    redis_obj.publish('chat', json.dumps({'message': str(message), 'socketid': str(socketid)}))


class CustomPrint():

    def __init__(self, socketid):
        self.old_stdout = sys.stdout  # save stdout
        self.socketid = socketid

    def write(self, text):
        text = text.rstrip()
        if len(text) == 0:
            return
        if (text == 'sleeping'):
            return

        log_to_terminal(text, self.socketid)


def classify_wrapper_redis(src_path, socketid, result_path):
    try:

        # PUSH job into redis classify queue

        redis_obj.publish(classify_channel_name, json.dumps(
            {'src_path': src_path, 'socketid': socketid, 'result_path': result_path}))
        log_to_terminal('Task Scheduled..Please Wait', socketid)

    except:
        log_to_terminal(str(traceback.format_exc()), socketid)


def classify_wrapper_local(src_path, socketid, result_path):
    classifyImages.delay(src_path, socketid, result_path)


def response_mimetype(request):
    if "application/json" in request.META['HTTP_ACCEPT']:
        return "application/json"
    else:
        return "text/plain"


class ClassifyCreateView(CreateView):
    model = Classify
    r = None
    socketid = None
    count_hits = 0
    fields = "__all__"

    def form_valid(self, form):
        self.r = redis_obj
        self.request.session.session_key
        socketid = self.request.POST['socketid-hidden']
        self.socketid = socketid

        try:
            # log_to_terminal('Logging user ip....', self.socketid)
            # client_address = self.request.META['REMOTE_ADDR']
            # client_address = self.request.environ.get('HTTP_X_FORWARDED_FOR')
            # log_to_terminal(client_address, self.socketid)

            self.object = form.save()
            all_files = self.request.FILES.getlist('file')
            data = {'files': []}

            fcountfile = open(os.path.join(conf.LOG_DIR, 'log_count.txt'), 'a')
            fcountfile.write(str(self.request.META.get('REMOTE_ADDR')) + '\n')
            fcountfile.close()

            self.count_hits += 1

        except:
            log_to_terminal(str(traceback.format_exc()), self.socketid)

        old_save_dir = os.path.dirname(conf.PIC_DIR)
        folder_name = str(shortuuid.uuid())
        save_dir = os.path.join(conf.PIC_DIR, folder_name)

        # Make the new directory based on time
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            os.makedirs(os.path.join(save_dir, 'results'))

        if len(all_files) == 1:
            log_to_terminal(str('Downloading Image...'), self.socketid)
        else:
            log_to_terminal(str('Downloading Images...'), self.socketid)

        for file in all_files:
            try:
                a = Picture()
                tick = time.time()
                strtick = str(tick).replace('.', '_')
                fileName, fileExtension = os.path.splitext(file.name)
                file.name = fileName + strtick + fileExtension
                a.file.save(file.name, file)
                file.name = a.file.name
                imgfile = Image.open(os.path.join(old_save_dir, file.name))
                size = (500, 500)
                imgfile.thumbnail(size, Image.ANTIALIAS)
                imgfile.save(os.path.join(save_dir, file.name))
                thumbPath = os.path.join(folder_name, file.name)
                data['files'].append({
                    'url': conf.PIC_URL + thumbPath,
                    'name': file.name,
                    'type': 'image/png',
                    'thumbnailUrl': conf.PIC_URL + thumbPath,
                    'size': 0,
                })
            except:
                log_to_terminal(str(traceback.format_exc()), self.socketid)

        if len(all_files) == 1:
            log_to_terminal(str('Processing Image...'), self.socketid)
        else:
            log_to_terminal(str('Processing Images...'), self.socketid)

        time.sleep(.5)

        # TODO: Make this threaded
        # This is for running it locally ie on Godel
        classify_wrapper_local(save_dir, socketid, os.path.join(conf.PIC_URL, folder_name))

        # This is for posting it on Redis - ie to Rosenblatt
        # classify_wrapper_redis(job_directory, socketid, result_folder)

        response = JSONResponse(data, {}, response_mimetype(self.request))
        response['Content-Disposition'] = 'inline; filename=files.json'
        return response

    def get_context_data(self, **kwargs):
        context = super(ClassifyCreateView, self).get_context_data(**kwargs)
        context['pictures'] = Classify.objects.all()
        return context


class ClassifyDeleteView(DeleteView):
    model = Classify

    def delete(self, request, *args, **kwargs):
        """
        This does not actually delete the file, only the database record.  But
        that is easy to implement.
        """
        self.object = self.get_object()
        self.object.delete()
        if request.is_ajax():
            response = JSONResponse(True, {}, response_mimetype(self.request))
            response['Content-Disposition'] = 'inline; filename=files.json'
            return response
        else:
            return HttpResponseRedirect('/upload/new')


class JSONResponse(HttpResponse):
    """JSON response class."""

    def __init__(self, obj='', json_opts={}, mimetype="application/json", *args, **kwargs):
        content = json.dumps(obj, **json_opts)
        super(JSONResponse, self).__init__(content, mimetype, *args, **kwargs)


@csrf_exempt
def demoClassify(request):
    post_dict = parser.parse(request.POST.urlencode())
    try:
        if not os.path.exists(demo_log_file):
            log_file = open(demo_log_file, 'w')
        else:
            log_file = open(demo_log_file, 'a')

        if 'src' not in post_dict:
            data = {'error': 'NoImageSelected'}
        else:
            data = {'info': 'Processing'}
            result_path = post_dict['src']
            imgname = basename(urlparse(result_path).path)

            image_path = os.path.join(conf.LOCAL_DEMO_PIC_DIR, imgname)
            log_to_terminal('Processing image...', post_dict['socketid'])

            # This is for running it locally ie on Godel
            classify_wrapper_local(image_path, post_dict['socketid'], result_path)

            # This is for posting it on Redis - ie to Rosenblatt
            # classify_wrapper_redis(image_path, post_dict['socketid'], result_path)
            data = {'info': 'Completed'}
        try:
            client_address = request.META['REMOTE_ADDR']
            log_file.write('Demo classify request from IP:' + client_address)
            log_file.close()
        except:
            log_file.write('Exception when finding client ip:' + str(traceback.format_exc()) + '\n')
            log_file.close()

        response = JSONResponse(data, {}, response_mimetype(request))
        response['Content-Disposition'] = 'inline; filename=files.json'
        return response

    except:
        data = {'result': str(traceback.format_exc())}
        response = JSONResponse(data, {}, response_mimetype(request))
        response['Content-Disposition'] = 'inline; filename=files.json'
        return response
