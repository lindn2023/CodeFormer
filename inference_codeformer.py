import os
import cv2
import argparse
import glob
import torch
from torchvision.transforms.functional import normalize
from basicsr.utils import imwrite, img2tensor, tensor2img
from facelib.utils.face_restoration_helper import FaceRestoreHelper
import torch.nn.functional as F

from basicsr.utils.registry import ARCH_REGISTRY

if __name__ == '__main__':
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    parser = argparse.ArgumentParser()

    parser.add_argument('--w', type=float, default=0.5, help='Balance the quality and fidelity')
    parser.add_argument('--upscale', type=int, default=2, help='The final upsampling scale of the image. Default: 2')
    parser.add_argument('--test_path', type=str, default='./inputs/cropped_faces')
    parser.add_argument('--has_aligned', action='store_true', help='Input are cropped and aligned faces')
    parser.add_argument('--only_center_face', action='store_true', help='Only restore the center face')
    parser.add_argument('--draw_box', action='store_true')

    args = parser.parse_args()
    if args.test_path.endswith('/'):  # solve when path ends with /
        args.test_path = args.test_path[:-1]

    w = args.w
    result_root = f'results/{os.path.basename(args.test_path)}_{w}'

    # set up the Network
    net = ARCH_REGISTRY.get('CodeFormer')(dim_embd=512, codebook_size=1024, n_head=8, n_layers=9, 
                                            connect_list=['32', '64', '128', '256']).to(device)

    ckpt_path = 'weights/CodeFormer/codeformer.pth'
    checkpoint = torch.load(ckpt_path)['params_ema']
    net.load_state_dict(checkpoint)
    net.eval()

    # large det_model: 'YOLOv5l', 'retinaface_resnet50'
    # small det_model: 'YOLOv5n', 'retinaface_mobile0.25'
    face_helper = FaceRestoreHelper(
        args.upscale,
        face_size=512,
        crop_ratio=(1, 1),
        det_model = 'YOLOv5l',
        save_ext='png',
        use_parse=True,
        device=device)


    # face_helper.init_dlib(args.detection_path, args.landmark5_path, args.landmark68_path)

    # scan all the jpg and png images
    for img_path in sorted(glob.glob(os.path.join(args.test_path, '*.[jp][pn]g'))):
        # clean all the intermediate results to process the next image
        face_helper.clean_all()

        img_name = os.path.basename(img_path)
        # if not '04' in img_name:
        #     continue 
        print(f'Processing: {img_name}')
        basename, ext = os.path.splitext(img_name)
        img = cv2.imread(img_path, cv2.IMREAD_COLOR)

        if args.has_aligned: 
            # the input faces are already cropped and aligned
            img = cv2.resize(img, (512, 512), interpolation=cv2.INTER_LINEAR)
            face_helper.cropped_faces = [img]
        else:
            face_helper.read_image(img)
            # get face landmarks for each face
            num_det_faces = face_helper.get_face_landmarks_5(
                only_center_face=args.only_center_face, resize=640, eye_dist_threshold=5)
            print(f'\tdetect {num_det_faces} faces')
            # align and warp each face
            face_helper.align_warp_face()

        # face restoration for each cropped face
        for idx, cropped_face in enumerate(face_helper.cropped_faces):
            # prepare data
            cropped_face_t = img2tensor(cropped_face / 255., bgr2rgb=True, float32=True)
            normalize(cropped_face_t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
            cropped_face_t = cropped_face_t.unsqueeze(0).to(device)

            try:
                with torch.no_grad():
                    output = net(cropped_face_t, w=w, adain=True)[0]
                    restored_face = tensor2img(output, rgb2bgr=True, min_max=(-1, 1))
                del output
                torch.cuda.empty_cache()
            except Exception as error:
                print(f'\tFailed inference for CodeFormer: {error}')
                restored_face = tensor2img(cropped_face_t, rgb2bgr=True, min_max=(-1, 1))

            restored_face = restored_face.astype('uint8')
            face_helper.add_restored_face(restored_face)

        # paste_back
        if not args.has_aligned:
            bg_img = None
            face_helper.get_inverse_affine(None)
            # paste each restored face to the input image
            restored_img = face_helper.paste_faces_to_input_image(upsample_img=bg_img, draw_box=args.draw_box)

        # save faces
        for idx, (cropped_face, restored_face) in enumerate(zip(face_helper.cropped_faces, face_helper.restored_faces)):
            # save cropped face
            if not args.has_aligned: 
                save_crop_path = os.path.join(result_root, 'cropped_faces', f'{basename}_{idx:02d}.png')
                imwrite(cropped_face, save_crop_path)
            # save restored face
            if args.has_aligned:
                save_face_name = f'{basename}.png'
            else:
                save_face_name = f'{basename}_{idx:02d}.png'
            save_restore_path = os.path.join(result_root, 'restored_faces', save_face_name)
            imwrite(restored_face, save_restore_path)

        # save restored img
        if not args.has_aligned and restored_img is not None:
            save_restore_path = os.path.join(result_root, 'final_results', f'{basename}.png')
            imwrite(restored_img, save_restore_path)

    print(f'\nAll results are saved in {result_root}')